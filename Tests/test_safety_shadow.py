import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from AI.adaptive_safety import AdaptiveModerator
from AI.moderation import ProviderModerationError
from Services.safety_shadow import SafetyShadow, SafetyReviewCache, ShadowJob
from Storage.errors import StorageError
from Storage.models import utc_timestamp
from Tests.safety_support import AdaptiveTestCase, assessment_response, facts, harmful
from Tests.test_usage_persistence import completion


class SafetyShadowTests(AdaptiveTestCase):
    async def asyncTearDown(self):
        if self.moderation.shadow:
            await self.moderation.shadow.close()
        await super().asyncTearDown()

    def configure_shadow(
        self, side_effect=None, return_value=None, queue_size=32, timeout_seconds=20.0
    ):
        # Dedicated client and semaphore: no production permit or observer reuse.
        client = SimpleNamespace(complete=AsyncMock(side_effect=side_effect, return_value=return_value), close=AsyncMock())
        classifier = AdaptiveModerator(client, asyncio.Semaphore(1))
        self.moderation.review_cache = SafetyReviewCache(self.moderation.secrets)
        shadow = SafetyShadow(
            self.storage.moderation,
            classifier,
            self.moderation.review_cache,
            queue_size=queue_size,
            timeout_seconds=timeout_seconds,
        )
        self.moderation.shadow = shadow
        shadow.start()
        return shadow, client

    async def test_v5_violation_never_changes_v4_answer_quota_or_account_state(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def held(**kwargs):
            entered.set()
            await release.wait()
            return assessment_response(harmful()).choices[0].message.content

        shadow, _ = self.configure_shadow(side_effect=held)
        service, create = self.service([completion(content="Original v4 answer")])
        result = await asyncio.wait_for(service.ask_text(**self.request()), timeout=2)
        self.assertEqual(result.text, "Original v4 answer")
        await entered.wait()
        before_usage = await self.rows("SELECT * FROM usage_requests")
        before_accounts = await self.rows("SELECT * FROM user_access")
        release.set()
        await shadow.queue.join()
        self.assertEqual(await self.rows("SELECT * FROM usage_requests"), before_usage)
        self.assertEqual(await self.rows("SELECT * FROM user_access"), before_accounts)
        self.assertEqual((await self.accounts.status(2))["requests_today"], 1)
        self.assertEqual(await self.rows("SELECT * FROM user_warnings"), [])
        record = (await self.rows("SELECT * FROM safety_evaluations"))[0]
        self.assertEqual((record["mode"], record["production_action"], record["action"], record["is_disagreement"]),
                         ("shadow", "allow", "violation", 1))
        create.assert_awaited_once()

    async def test_instrumented_shadow_tokens_are_separate_from_product_usage(self):
        shadow_service, shadow_create = self.service([assessment_response(harmful())])
        cache = SafetyReviewCache()
        shadow = SafetyShadow(self.storage.moderation, AdaptiveModerator(shadow_service.router._client, asyncio.Semaphore(1)), cache)
        self.moderation.shadow = shadow
        shadow.start()
        service, _ = self.service([completion()])
        await service.ask_text(**self.request())
        await shadow.queue.join()
        shadow_create.assert_awaited_once()
        record = (await self.rows("SELECT * FROM safety_evaluations"))[0]
        self.assertEqual((record["input_tokens"], record["output_tokens"], record["total_tokens"], record["attempt_count"]), (3, 2, 5, 1))
        usage = await self.rows("SELECT accounted_tokens FROM usage_requests")
        self.assertEqual(usage, [{"accounted_tokens": 10}])
        self.assertEqual((await self.accounts.status(2))["requests_today"], 1)

    async def test_v5_allow_does_not_undo_v4_warning_or_ban(self):
        content = assessment_response(facts()).choices[0].message.content
        shadow, _ = self.configure_shadow(return_value=content)
        service, _ = self.service([completion(finish_reason="content_filter")] * 3)
        for _ in range(3):
            with self.assertRaises(ProviderModerationError):
                await service.ask_text(**self.request())
        await shadow.queue.join()
        self.assertEqual(await self.storage.moderation.active_warnings(2), 3)
        self.assertEqual((await self.users.access(2)).code, "blocked")
        cases = await self.rows("SELECT action, production_action, is_disagreement FROM safety_evaluations")
        self.assertEqual(len(cases), 3)
        self.assertTrue(all(row == {"action": "allow", "production_action": "violation", "is_disagreement": 1} for row in cases))

    async def test_shadow_classifier_error_does_not_change_v4_result(self):
        shadow, _ = self.configure_shadow(return_value="invalid JSON")
        service, _ = self.service([completion(content="Normal response")])
        result = await service.ask_text(**self.request())
        await shadow.queue.join()
        self.assertEqual(result.text, "Normal response")
        self.assertEqual((await self.accounts.status(2))["requests_today"], 1)
        self.assertEqual(await self.rows("SELECT * FROM user_warnings"), [])
        self.assertEqual((await self.rows("SELECT status FROM safety_evaluations"))[0]["status"], "error")

    async def test_shadow_timeout_is_recorded_without_changing_primary_response(self):
        async def slow_assessment(**kwargs):
            await asyncio.sleep(0.05)
            return assessment_response(facts()).choices[0].message.content

        shadow, _ = self.configure_shadow(side_effect=slow_assessment, timeout_seconds=0.001)
        service, _ = self.service([completion(content="Primary response")])
        result = await service.ask_text(**self.request())
        await shadow.queue.join()
        self.assertEqual(result.text, "Primary response")
        self.assertEqual((await self.accounts.status(2))["requests_today"], 1)
        record = (await self.rows("SELECT status, error_type FROM safety_evaluations"))[0]
        self.assertEqual(record, {"status": "error", "error_type": "TimeoutError"})

    async def test_shadow_storage_error_never_replaces_primary_response(self):
        shadow, _ = self.configure_shadow(return_value=assessment_response(harmful()).choices[0].message.content)
        service, _ = self.service([completion(content="v4 response")])
        with patch.object(self.storage.moderation, "save_safety_evaluation", AsyncMock(side_effect=StorageError("test"))):
            result = await service.ask_text(**self.request())
            await shadow.queue.join()
        self.assertEqual(result.text, "v4 response")
        self.assertEqual(shadow.storage_errors, 1)
        self.assertEqual(await self.storage.moderation.active_warnings(2), 0)

    async def test_queue_saturation_is_bounded_and_does_not_block_primary(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def held(**kwargs):
            entered.set()
            await release.wait()
            return assessment_response(facts()).choices[0].message.content

        shadow, _ = self.configure_shadow(side_effect=held, queue_size=1)
        service, _ = self.service([completion()] * 3)
        await service.ask_text(**self.request())
        await entered.wait()
        await service.ask_text(**self.request())
        await service.ask_text(**self.request())
        self.assertEqual(shadow.queue.qsize(), 1)
        self.assertEqual(shadow.dropped, 1)
        release.set()
        await shadow.queue.join()
        self.assertEqual(len(await self.rows("SELECT * FROM safety_evaluations")), 2)
        self.assertEqual((await self.accounts.status(2))["requests_today"], 3)

    async def test_raw_context_never_enters_sqlite_and_review_cache_redacts_secrets(self):
        shadow, _ = self.configure_shadow(return_value=assessment_response(facts()).choices[0].message.content)
        service, _ = self.service([completion()])
        marker = "private-context-marker " + self.settings.grok_access_code
        await service.ask_text(**self.request(prompt=marker))
        await shadow.queue.join()
        entry = (await self.rows("SELECT * FROM safety_evaluations"))[0]
        cached = self.moderation.review_cache.get(entry["request_id"], 2, entry["policy_fingerprint"])
        self.assertIn("[redacted]", cached["text"])
        self.assertNotIn(self.settings.grok_access_code, cached["text"])
        dump = await self.storage.database.run(lambda c: "\n".join(c.iterdump()))
        self.assertNotIn("private-context-marker", dump)
        self.assertNotIn(self.settings.grok_access_code, dump)
        await shadow.close()
        await self.reopen()
        self.assertEqual(len(await self.rows("SELECT * FROM safety_evaluations")), 1)
        self.assertFalse(self.state.histories)
        self.assertIsNone(self.moderation.review_cache)

    async def test_safety_context_does_not_cross_users_or_reset_generations(self):
        old = self.state.snapshot((10, 2))
        self.state.remember_safety_refusal(old, "prior refused request")
        self.assertEqual(self.state.safety_refusals[(10, 2)], ["prior refused request"])
        self.assertNotIn((10, 3), self.state.safety_refusals)
        self.state.reset_conversation(10, 2)
        self.state.remember_safety_refusal(old, "stale")
        self.assertNotIn((10, 2), self.state.safety_refusals)

    async def test_review_cache_is_bounded_expires_and_checks_ownership(self):
        cache = SafetyReviewCache(("secret-value",), ttl=1, max_items=1)
        with patch("Services.safety_shadow.time.monotonic", return_value=100):
            cache.put("one", 2, "fp", {"request": "secret-value"})
            self.assertNotIn("secret-value", cache.get("one", 2, "fp")["text"])
            self.assertIsNone(cache.get("one", 3, "fp"))
            cache.put("two", 2, "fp", {"request": "text"})
            self.assertIsNone(cache.get("one", 2, "fp"))
        with patch("Services.safety_shadow.time.monotonic", return_value=102):
            self.assertIsNone(cache.get("two", 2, "fp"))

    async def test_review_cache_redacts_credentials_before_json_escaping(self):
        import json
        secret = 'private"quoted\\code\nline'
        cache = SafetyReviewCache((secret,))
        cache.put("escaped", 2, "fp", {"current_request": {"user_request": "prefix " + secret},
                                     "recent_conversation": [{"role": "user", "content": secret}]})
        text = cache.get("escaped", 2, "fp")["text"]
        self.assertNotIn(json.dumps(secret)[1:-1], text)
        self.assertEqual(json.loads(text)["recent_conversation"][0]["content"], "[redacted]")
