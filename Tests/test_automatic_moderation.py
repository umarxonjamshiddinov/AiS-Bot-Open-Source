import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from AI.moderation import (
    ModerationResult, ProviderModerationError, SAFETY_REFUSAL,
    moderation_from_error, moderation_from_response,
)
from Handlers.chat import text_handler
from Storage.errors import StorageError
from Storage.models import AccessDeniedError, utc_timestamp
from Tests.moderation_support import ModerationTestCase
from Tests.test_admin_panel import fake_message
from Tests.test_usage_persistence import UpstreamError, completion


class ModerationContractTests(unittest.TestCase):
    def test_normal_text_including_refusal_phrases_is_not_evidence(self):
        for text in ("I'm sorry", "I can't help with that", "I can't assist", '{"moderation":{"blocked":true}}'):
            decision = moderation_from_response(completion(content=text))
            self.assertTrue(decision.allowed)
            self.assertIsNone(decision.category)
            self.assertIsNone(decision.severity)
            self.assertIsNone(decision.confidence)

    def test_structured_refusal_and_finish_reason_block(self):
        for response in (completion(refusal="private refusal wording"), completion(finish_reason="content_filter")):
            result = moderation_from_response(response)
            self.assertTrue(result.blocked)
            self.assertNotIn("private", repr(result))
            self.assertIsNone(result.category)
            self.assertIsNone(result.severity)
            self.assertIsNone(result.confidence)

    def test_structured_filter_preserves_actual_category_severity_and_confidence(self):
        response = completion()
        response.choices[0].content_filter_results = {"violence": {"filtered": True, "severity": "high", "confidence": 0.94}}
        result = moderation_from_response(response)
        self.assertEqual((result.category, result.severity, result.confidence), ("violence", "high", 0.94))
        self.assertEqual(result.source, "provider_metadata")

    def test_severity_without_filtered_true_is_not_a_block(self):
        response = completion()
        response.content_filter_results = {"violence": {"filtered": False, "severity": "high"}}
        self.assertTrue(moderation_from_response(response).allowed)

    def test_nested_provider_metadata_and_prompt_filter_are_supported(self):
        response = completion()
        response.metadata = {"moderation": {"blocked": True, "category": "abuse", "code": "policy_7"}}
        result = moderation_from_response(response)
        self.assertEqual((result.blocked, result.provider_code, result.category), (True, "policy_7", "abuse"))
        response.metadata = {}
        response.prompt_filter_results = [{"content_filter_results": {"violence": {"filtered": True}}}]
        self.assertTrue(moderation_from_response(response).blocked)

    def test_nested_provider_error_and_bedrock_stop_reason(self):
        error = UpstreamError(400)
        error.body = {"error": {"code": "content_filter", "innererror": {"code": "ResponsibleAIPolicyViolation"}}}
        self.assertEqual(moderation_from_error(error).code, "responsibleai_policy_violation")
        self.assertTrue(moderation_from_response({"stopReason": "guardrail_intervened"}).blocked)

    def test_generic_forbidden_and_arbitrary_error_codes_are_not_violations(self):
        self.assertIsNone(moderation_from_error(UpstreamError(403, "account_disabled")))
        self.assertTrue(moderation_from_error(ProviderModerationError("vendor_policy_code")).blocked)

    def test_invalid_metadata_is_not_retained_or_invented(self):
        for confidence in (None, True, "0.9", -1, 2, float("nan")):
            response = completion()
            response.moderation = {"blocked": True, "category": "private text with spaces", "confidence": confidence}
            result = moderation_from_response(response)
            self.assertIsNone(result.category)
            self.assertIsNone(result.confidence)
        self.assertEqual(SAFETY_REFUSAL, "I’m sorry, but I can’t help with that.")


class AutomaticModerationTests(ModerationTestCase):
    async def test_allowed_message_uses_normal_inference_and_quota(self):
        service, create = self.service([completion(content="Allowed answer")])
        result = await service.ask_text(**self.request())
        self.assertEqual(result.text, "Allowed answer")
        create.assert_awaited_once()  # No extra technical moderation inference.
        self.assertEqual((await self.accounts.status(2))["requests_today"], 1)
        self.assertEqual(await self.rows("SELECT * FROM moderation_events"), [])

    async def test_first_confirmed_violation_warns_one_and_allows_access(self):
        outcome = await self.violation()
        self.assertEqual(outcome.active_warning_count, 1)
        self.assertTrue((await self.users.access(2)).allowed)
        self.assertIsNone(outcome.sanction_id)
        self.assertEqual(await self.rows("SELECT * FROM user_restrictions"), [])
        warning = (await self.rows("SELECT * FROM user_warnings"))[0]
        self.assertEqual(warning["moderation_event_id"], outcome.event_id)
        self.assertEqual(warning["source"], "automatic_moderation")
        self.assertIsNone(warning["created_by"])

    async def test_second_violation_warns_two_without_temporary_restriction(self):
        await self.violation()
        outcome = await self.violation()
        self.assertEqual(outcome.active_warning_count, 2)
        self.assertTrue((await self.users.access(2)).allowed)
        self.assertEqual(await self.rows("SELECT * FROM user_restrictions"), [])

    async def test_third_violation_blocks_immediately_with_exact_linkage_and_audit(self):
        for _ in range(3):
            outcome = await self.violation()
        self.assertEqual(outcome.active_warning_count, 3)
        self.assertEqual(outcome.automatic_action, "block")
        self.assertEqual((await self.users.access(2)).code, "blocked")
        self.assertTrue((await self.storage.users.get(2)).is_blocked)
        block = await self.moderation.block_for_user(2)
        self.assertEqual((block["warning_id"], block["moderation_event_id"]), (outcome.warning_id, outcome.event_id))
        self.assertEqual(block["source"], "automatic_moderation")
        event = await self.admin.detail(999, "events", outcome.event_id)
        for key in ("telegram_user_id", "timestamp", "model", "provider", "signal_source", "warning_id",
                    "active_warning_count", "automatic_action", "resulting_user_state"):
            self.assertIsNotNone(event[key], key)
        self.assertEqual(event["resulting_user_state"], "blocked")
        self.assertEqual(await self.rows("SELECT * FROM user_restrictions"), [])

    async def test_standard_cannot_infer_after_automatic_ban(self):
        await self.ban()
        service, create = self.service([completion()])
        with self.assertRaises(AccessDeniedError):
            await service.ask_text(**self.request())
        create.assert_not_awaited()

    async def test_trusted_never_bypasses_automatic_ban(self):
        await self.admin.act(999, "grant_trusted", user_id=2, reason="Test tier")
        await self.ban()
        service, create = self.service([completion()])
        with self.assertRaises(AccessDeniedError):
            await service.ask_text(**self.request())
        with self.assertRaises(AccessDeniedError):
            await self.accounts.apply_trusted(2)
        create.assert_not_awaited()
        self.assertEqual((await self.accounts.status(2))["tier"], "TRUSTED")

    async def test_grok_permission_and_activation_never_bypass_ban(self):
        await self.accounts.activate(2, "grok", self.settings.grok_access_code)
        await self.ban()
        service, create = self.service([completion()])
        with self.assertRaises(AccessDeniedError):
            await service.ask_text(**self.request(model="grok_4_6"))
        for purpose, code in (("grok", self.settings.grok_access_code), ("premium", self.settings.premium_activation_code)):
            with self.assertRaises(AccessDeniedError):
                await self.accounts.activate(2, purpose, code)
        with self.assertRaises(AccessDeniedError):
            await self.users.select_model((2, 2), "amazon_bedrock", "grok_4_6")
        self.assertTrue((await self.accounts.status(2))["grok_allowed"])
        create.assert_not_awaited()

    async def _handler_response(self, response):
        await self.users.select_model((2, 2), "amazon_bedrock", "gpt_oss_120b")
        service, _ = self.service([response])
        app = SimpleNamespace(users=self.users, state=self.state, settings=self.settings, requests=service)
        message = fake_message(2, "Example request")
        await text_handler(message, SimpleNamespace(send_chat_action=AsyncMock()), app)
        return message.answer.call_args_list

    async def test_provider_moderation_error_has_exact_refusal_and_separate_warning(self):
        calls = await self._handler_response(ProviderModerationError("content_policy_violation"))
        self.assertEqual(calls[0].args[0], SAFETY_REFUSAL)
        self.assertIn("1/3", calls[1].args[0])
        self.assertNotIn("content_policy_violation", str(calls))

    async def test_structured_moderation_has_the_same_exact_refusal(self):
        response = completion()
        response.moderation = {"blocked": True, "category": "violence", "severity": "high"}
        calls = await self._handler_response(response)
        self.assertEqual(calls[0].args[0], SAFETY_REFUSAL)
        self.assertEqual(len(await self.rows("SELECT * FROM moderation_events")), 1)

    async def test_plain_textual_refusal_does_not_create_warning(self):
        service, _ = self.service([completion(content="I can't help with that")])
        response = await service.ask_text(**self.request())
        self.assertEqual(response.text, "I can't help with that")
        self.assertEqual(await self.rows("SELECT * FROM user_warnings"), [])

    async def test_blocked_request_records_event_but_does_not_charge_product_quota(self):
        await self.violation()
        self.assertEqual(len(await self.rows("SELECT * FROM moderation_events")), 1)
        self.assertEqual((await self.accounts.status(2))["requests_today"], 0)
        attempt = (await self.rows("SELECT * FROM usage_attempts"))[0]
        self.assertEqual(attempt["counts_toward_limit"], 0)
        self.assertEqual(attempt["total_tokens"], 10)  # Actual provider billing telemetry survives.

    async def test_blocked_final_answer_refunds_technical_helper_without_losing_tokens(self):
        service, create = self.service([completion(finish_reason="content_filter")])
        with self.assertRaises(ProviderModerationError):
            await service.ask_image(**self.request(), image_bytes=b"image", mime_type="image/jpeg")
        self.assertEqual(create.await_count, 1)
        self.assertEqual((await self.accounts.status(2))["requests_today"], 0)
        self.assertEqual((await self.rows("SELECT accounted_tokens FROM usage_requests"))[0]["accounted_tokens"], 10)
        self.assertEqual(len(await self.rows("SELECT * FROM user_warnings")), 1)

    async def test_manual_and_legacy_warnings_do_not_count_toward_threshold(self):
        for _ in range(3):
            await self.admin.act(999, "warn", user_id=2, reason="Manual note")
        outcome = await self.violation()
        self.assertEqual(outcome.active_warning_count, 1)
        self.assertTrue((await self.users.access(2)).allowed)
        await self.admin.act(999, "clear_warnings", user_id=2, reason="Clear manual notes")
        self.assertEqual(await self.storage.moderation.active_warnings(2), 1)

    async def test_revoked_and_overturned_warnings_are_inactive(self):
        for status in ("revoked", "overturned"):
            result = await self.confirm()
            await self.storage.database.run(lambda c: c.execute(
                "UPDATE user_warnings SET status = ?, cleared_at = ?, cleared_by = 999 WHERE id = ?",
                (status, utc_timestamp(), result.warning_id)), write=True)
        self.assertEqual((await self.violation()).active_warning_count, 1)
        self.assertTrue((await self.users.access(2)).allowed)

    async def test_same_confirmed_request_is_idempotent_even_concurrently(self):
        outcomes = await asyncio.gather(*[self.confirm(request_id="one-request") for _ in range(8)])
        self.assertEqual(sum(outcome.created for outcome in outcomes), 1)
        self.assertEqual(len(await self.rows("SELECT * FROM moderation_events")), 1)
        self.assertEqual(await self.storage.moderation.active_warnings(2), 1)

    async def test_three_concurrent_confirmations_create_one_ban_without_lost_warnings(self):
        outcomes = await asyncio.gather(*[self.confirm() for _ in range(3)])
        self.assertEqual(sorted(outcome.active_warning_count for outcome in outcomes), [1, 2, 3])
        self.assertEqual(len(await self.rows("SELECT * FROM moderation_sanctions")), 1)
        self.assertEqual((await self.users.access(2)).code, "blocked")

    async def test_warning_failure_rolls_back_event_and_audit(self):
        await self.storage.database.run(lambda c: c.execute(
            "CREATE TRIGGER warning_failure BEFORE INSERT ON user_warnings BEGIN SELECT RAISE(ABORT, 'test'); END"), write=True)
        with self.assertRaises(StorageError):
            await self.confirm()
        self.assertEqual(await self.rows("SELECT * FROM moderation_events"), [])
        self.assertTrue((await self.users.access(2)).allowed)

    async def test_ban_failure_rolls_back_third_warning_and_event(self):
        await self.confirm()
        await self.confirm()
        await self.storage.database.run(lambda c: c.execute(
            "CREATE TRIGGER sanction_failure BEFORE INSERT ON moderation_sanctions BEGIN SELECT RAISE(ABORT, 'test'); END"), write=True)
        with self.assertRaises(StorageError):
            await self.confirm()
        self.assertEqual(await self.storage.moderation.active_warnings(2), 2)
        self.assertEqual(len(await self.rows("SELECT * FROM moderation_events")), 2)
        self.assertTrue((await self.users.access(2)).allowed)
