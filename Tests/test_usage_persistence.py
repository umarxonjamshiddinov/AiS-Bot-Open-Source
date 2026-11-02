import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from AI.client import AISClient, AISAPIError
from AI.router import AIRouter, StaleConversationError
from AI.telemetry import ProviderModerationError
from Services.requests import AIRequestService
from Storage.errors import StorageError
from Storage.models import AccessDeniedError, utc_timestamp
from Tests.storage_support import StorageTestCase
from Tests.test_routing import FakeSearch


def completion(*, tokens=(4, 6, 10), content="Test response", finish_reason="stop", refusal=None):
    usage = None if tokens is None else SimpleNamespace(
        prompt_tokens=tokens[0], completion_tokens=tokens[1], total_tokens=tokens[2])
    return SimpleNamespace(usage=usage, choices=[SimpleNamespace(
        finish_reason=finish_reason, message=SimpleNamespace(content=content, refusal=refusal))])


class UpstreamError(RuntimeError):
    def __init__(self, status=500, code=None):
        super().__init__("sensitive-error-body-with-prompt-and-credentials")
        self.status_code = status
        self.code = code


class UsagePersistenceTests(StorageTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.add_user()

    def service(self, outcomes, retries=0):
        client = AISClient(api_key="fake", base_url="https://ais.example/v1",
                              timeout=5, retry_count=retries)
        create = AsyncMock(side_effect=outcomes)
        client.client_for = lambda model: SimpleNamespace(chat=SimpleNamespace(
            completions=SimpleNamespace(create=create)))
        router = AIRouter(client=client, search=FakeSearch(), state=self.state,
                          max_concurrent_requests=4, default_max_output_tokens=1024)
        service = AIRequestService(router, self.users, self.storage.usage, self.moderation, self.state)
        return service, create

    def request(self, user_id=2, model="gpt_oss_120b", prompt="private-prompt-not-for-storage"):
        key = (10, user_id)
        return dict(conversation_key=key, conversation_id=self.state.current_conversation_id(key),
                    model_key=model, provider="amazon_bedrock", prompt=prompt)

    async def test_success_persists_actual_usage_without_text_and_survives_restart(self):
        service, create = self.service([completion(content="private-response-not-for-storage")])
        response = await service.ask_text(**self.request())
        self.assertEqual(response.text, "private-response-not-for-storage")
        create.assert_awaited_once()
        await self.reopen()
        row = (await self.rows("SELECT * FROM usage_requests"))[0]
        self.assertEqual(row["status"], "succeeded")
        self.assertEqual((row["input_tokens"], row["output_tokens"], row["total_tokens"]), (4, 6, 10))
        self.assertEqual(row["accounted_tokens"], 10)
        self.assertEqual(row["usage_complete"], 1)
        self.assertGreaterEqual(row["latency_ms"], 0)
        self.assertEqual(await self.rows("SELECT * FROM moderation_events"), [])
        dump = await self.storage.database.run(lambda connection: "\n".join(connection.iterdump()))
        self.assertNotIn("private-prompt", dump)
        self.assertNotIn("private-response", dump)

    async def test_absent_partial_or_invalid_usage_stays_unknown(self):
        cases = (None, (4, None, None), (True, -5, "10"))
        for tokens in cases:
            service, _ = self.service([completion(tokens=tokens)])
            await service.ask_text(**self.request())
        rows = await self.rows("SELECT * FROM usage_requests ORDER BY timestamp")
        self.assertIsNone(rows[0]["input_tokens"])
        self.assertIsNone(rows[0]["accounted_tokens"])
        self.assertEqual(rows[1]["input_tokens"], 4)
        self.assertIsNone(rows[1]["total_tokens"])
        self.assertIsNone(rows[2]["input_tokens"])
        self.assertIsNone(rows[2]["output_tokens"])
        self.assertTrue(all(row["usage_complete"] == 0 for row in rows))

    async def test_gateway_image_usage_is_recorded_once(self):
        service, create = self.service([completion(tokens=(10, 20, 30))])
        result = await service.ask_image(**self.request(), image_bytes=b"image", mime_type="image/jpeg")
        self.assertEqual(result.route, "ais_vision")
        self.assertEqual(create.await_count, 1)
        attempts = await self.rows("SELECT * FROM usage_attempts ORDER BY timestamp")
        self.assertEqual([row["model"] for row in attempts], ["gpt_oss_120b"])
        request = (await self.rows("SELECT * FROM usage_requests"))[0]
        self.assertEqual((request["input_tokens"], request["output_tokens"], request["total_tokens"]), (10, 20, 30))

    async def test_failed_gateway_image_does_not_invent_helper_usage(self):
        service, create = self.service([UpstreamError()])
        with self.assertRaises(AISAPIError):
            await service.ask_image(**self.request(), image_bytes=b"image", mime_type="image/jpeg")
        create.assert_awaited_once()
        row = (await self.rows("SELECT * FROM usage_requests"))[0]
        self.assertEqual(row["status"], "error")
        self.assertIsNone(row["total_tokens"])
        self.assertEqual(row["error_code"], "http_500")

    async def test_rate_limit_is_not_retried_by_cloud_bot(self):
        service, create = self.service([UpstreamError(429), completion()], retries=2)
        with self.assertRaises(AISAPIError):
            await service.ask_text(**self.request())
        create.assert_awaited_once()
        attempts = await self.rows("SELECT * FROM usage_attempts")
        self.assertEqual([row["status"] for row in attempts], ["error"])

    async def test_failed_metrics_write_never_retries_a_successful_paid_call(self):
        service, create = self.service([completion()], retries=2)
        with patch.object(self.storage.usage, "finish_attempt", AsyncMock(side_effect=StorageError("failed"))):
            with self.assertRaises(StorageError):
                await service.ask_text(**self.request())
        create.assert_awaited_once()
        row = (await self.rows("SELECT * FROM usage_requests"))[0]
        self.assertEqual(row["status"], "error")

    async def test_provider_moderation_signals_are_recorded_and_never_enter_history(self):
        for outcome in (completion(finish_reason="content_filter"), completion(refusal="sensitive-refusal"),
                        UpstreamError(400, "content_policy_violation")):
            service, _ = self.service([outcome])
            with self.assertRaises(ProviderModerationError):
                await service.ask_text(**self.request())
        events = await self.admin.events(999, 2)
        self.assertEqual({row["code"] for row in events}, {"content_filter", "provider_refusal", "content_policy_violation"})
        self.assertTrue(all(row["source"] == "provider" and row["action"] == "block" for row in events))
        self.assertTrue(all(row["request_id"] for row in events))
        self.assertFalse(any(self.state.histories.values()))
        self.assertTrue(all(row["status"] == "blocked" for row in await self.rows("SELECT * FROM usage_requests")))
        dump = await self.storage.database.run(lambda connection: "\n".join(connection.iterdump()))
        self.assertNotIn("sensitive-refusal", dump)

    async def test_arbitrary_forbidden_http_error_is_not_a_moderation_finding(self):
        service, _ = self.service([UpstreamError(403, "upstream-secret-code")])
        with self.assertRaises(AISAPIError):
            await service.ask_text(**self.request())
        self.assertEqual(await self.admin.events(999, 2), [])
        row = (await self.rows("SELECT * FROM usage_requests"))[0]
        self.assertEqual(row["error_code"], "http_403")

    async def test_blocked_and_limited_requests_do_not_reach_upstream(self):
        service, create = await self._blocked_service()
        with self.assertRaises(AccessDeniedError):
            await service.ask_text(**self.request())
        create.assert_not_awaited()
        self.assertEqual((await self.rows("SELECT * FROM usage_requests"))[0]["status"], "blocked")

    async def _blocked_service(self):
        await self.admin.act(999, "block", user_id=2, reason="Ban")
        return self.service([completion()])

    async def test_cancellation_is_durable(self):
        started = asyncio.Event()

        async def wait_for_cancel(**kwargs):
            started.set()
            await asyncio.Event().wait()

        service, _ = self.service(wait_for_cancel)
        task = asyncio.create_task(service.ask_text(**self.request()))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual((await self.rows("SELECT * FROM usage_attempts"))[0]["status"], "cancelled")
        self.assertEqual((await self.rows("SELECT * FROM usage_requests"))[0]["status"], "cancelled")

    async def test_ban_during_helper_suppresses_delivery_and_next_inference(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def held_completion(**kwargs):
            started.set()
            await release.wait()
            return completion()

        service, create = self.service(held_completion)
        task = asyncio.create_task(service.ask_image(**self.request(), image_bytes=b"i", mime_type="image/jpeg"))
        await started.wait()
        await self.admin.act(999, "block", user_id=2, reason="Blocked while running")
        release.set()
        with self.assertRaises(StaleConversationError):
            await task
        self.assertEqual(create.await_count, 1)
        self.assertFalse(self.state.histories)
        row = (await self.rows("SELECT * FROM usage_requests"))[0]
        self.assertEqual(row["status"], "stale")
        self.assertEqual(row["accounted_tokens"], 10)

    async def test_concurrent_request_traces_do_not_mix_users(self):
        async def delayed(**kwargs):
            tokens = int(kwargs["messages"][-1]["content"])
            await asyncio.sleep(0.01)
            return completion(tokens=(tokens, 0, tokens))

        for user_id in (3, 4, 5):
            await self.add_user(user_id)
        service, _ = self.service(delayed)
        await asyncio.gather(*[service.ask_text(**self.request(user_id=n, prompt=str(n))) for n in (2, 3, 4, 5)])
        for row in await self.rows("SELECT * FROM usage_requests"):
            self.assertEqual(row["telegram_user_id"], row["total_tokens"])

    async def test_crash_recovery_keeps_usage_already_accounted(self):
        await self.storage.usage.start_request(request_id="r", user_id=2, timestamp=utc_timestamp(), model="m", provider="p")
        await self.storage.usage.start_attempt(attempt_id="completed", request_id="r", user_id=2,
                                              timestamp=utc_timestamp(), model="m", provider="p")
        await self.storage.usage.finish_attempt(attempt_id="completed", status="succeeded", latency_ms=1,
            input_tokens=1, output_tokens=2, total_tokens=3, error_type=None, error_code=None)
        await self.storage.usage.start_attempt(attempt_id="pending", request_id="r", user_id=2,
                                              timestamp=utc_timestamp(), model="m", provider="p")
        self.assertEqual((await self.rows("SELECT usage_complete FROM usage_requests"))[0]["usage_complete"], 0)
        await self.reopen()
        self.assertEqual(await self.storage.usage.recover_interrupted(utc_timestamp()), 1)
        row = (await self.rows("SELECT * FROM usage_requests"))[0]
        self.assertEqual(row["status"], "interrupted")
        self.assertEqual(row["accounted_tokens"], 3)
        self.assertIsNone(row["total_tokens"])
        self.assertEqual((await self.rows("SELECT status FROM usage_attempts WHERE attempt_id = 'pending'"))[0]["status"], "interrupted")
