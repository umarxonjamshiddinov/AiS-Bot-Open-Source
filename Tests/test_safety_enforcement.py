import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

from AI.input_moderation import ModerationUnavailableError
from AI.moderation import ProviderModerationError, SAFETY_REFUSAL
from AI.safety_policy import safe_completion_mode
from Handlers.chat import text_handler
from Tests.safety_support import AdaptiveTestCase, assessment_response, facts, harmful
from Tests.test_admin_panel import fake_message
from Tests.test_usage_persistence import completion


class AdaptiveEnforcementTests(AdaptiveTestCase):
    async def test_default_policy_remains_v4_even_when_v5_classifier_exists(self):
        service, create = self.adaptive_service([completion(content="v4 answer")])
        result = await service.ask_text(**self.request())
        self.assertEqual(result.text, "v4 answer")
        create.assert_awaited_once()
        self.assertEqual((await self.storage.moderation.policy_state())["policy_version"], "ais_safety_v1")
        self.assertEqual(await self.rows("SELECT * FROM safety_evaluations"), [])

    async def test_refuse_is_exact_and_nonpunitive_without_product_inference(self):
        service, create = self.adaptive_service([assessment_response(harmful(confidence=.75))])
        await self.activate()
        await self.users.select_model((2, 2), "amazon_bedrock", "gemma_3_12b_it")
        app = SimpleNamespace(users=self.users, state=self.state, settings=self.settings, requests=service)
        message = fake_message(2, "Risky request needing clarification")
        await text_handler(message, SimpleNamespace(send_chat_action=AsyncMock()), app)
        message.answer.assert_awaited_once()
        self.assertEqual(message.answer.call_args.args[0], SAFETY_REFUSAL)
        create.assert_awaited_once()
        self.assertTrue((await self.users.access(2)).allowed)
        self.assertEqual(await self.storage.moderation.active_warnings(2), 0)
        self.assertEqual((await self.accounts.status(2))["requests_today"], 0)
        event = (await self.admin.events(999, 2))[0]
        self.assertEqual((event["policy_action"], event["warning_id"], event["confidence_source"]), ("refuse", None, "model_estimate"))

    async def test_three_v5_violations_use_existing_warning_ban_appeal_workflow(self):
        service, create = self.adaptive_service([assessment_response(harmful(model_decision="allow"))] * 3)
        await self.activate()
        for count in (1, 2, 3):
            with self.assertRaises(ProviderModerationError) as caught:
                await service.ask_text(**self.request())
            self.assertEqual(caught.exception.outcome.active_warning_count, count)
            self.assertEqual((await self.users.access(2)).allowed, count < 3)
        self.assertEqual(create.await_count, 3)
        self.assertEqual((await self.accounts.status(2))["requests_today"], 0)
        self.assertEqual(await self.rows("SELECT * FROM user_restrictions"), [])
        events = await self.admin.events(999, 2)
        self.assertTrue(all(e["policy_version"] == "ais_safety_v2" and e["policy_action"] == "violation" for e in events))
        appeal = await self.submit_appeal()
        await self.admin.act(999, "approve_appeal", appeal_id=appeal["id"], reason="One mistaken classification")
        self.assertEqual(await self.storage.moderation.active_warnings(2), 2)
        self.assertTrue((await self.users.access(2)).allowed)

    async def test_safe_complete_injects_only_fixed_instruction_and_resets_task_context(self):
        value = facts(category="cyber_abuse", intent="defensive_security", authorization="authorized", severity="low")
        service, create = self.adaptive_service([assessment_response(value), completion(content="Defensive answer"), completion(content="v4 answer")])
        await self.activate()
        result = await service.ask_text(**self.request())
        self.assertEqual(result.text, "Defensive answer")
        system_prompt = create.call_args_list[1].kwargs["messages"][0]["content"]
        self.assertIn("AIS SAFE COMPLETION REQUIREMENT", system_prompt)
        self.assertIn("bounded, defensive", system_prompt)
        self.assertIsNone(safe_completion_mode.get())
        self.assertEqual((await self.accounts.status(2))["requests_today"], 1)
        await self.admin.act(999, "rollback_safety_v4", reason="Explicit operator rollback")
        await service.ask_text(**self.request())
        self.assertNotIn("AIS SAFE COMPLETION REQUIREMENT", create.call_args_list[2].kwargs["messages"][0]["content"])

    async def test_allow_does_not_append_safe_completion_constraints(self):
        service, create = self.adaptive_service([assessment_response(facts()), completion()])
        await self.activate()
        await service.ask_text(**self.request())
        self.assertNotIn("AIS SAFE COMPLETION REQUIREMENT", create.call_args_list[1].kwargs["messages"][0]["content"])
        self.assertEqual((await self.accounts.status(2))["requests_today"], 1)

    async def test_provider_refusal_after_v5_allow_does_not_invent_intent_or_warning(self):
        service, _ = self.adaptive_service([assessment_response(facts()), completion(refusal="Provider refusal")])
        await self.activate()
        with self.assertRaises(ProviderModerationError) as caught:
            await service.ask_text(**self.request())
        self.assertEqual(caught.exception.result.effective_action, "refuse")
        self.assertEqual(await self.rows("SELECT * FROM user_warnings"), [])
        self.assertEqual((await self.accounts.status(2))["requests_today"], 0)

    async def test_invalid_assessment_fails_without_warning_or_product_call(self):
        service, create = self.adaptive_service([completion(content="I cannot help")] * 2)
        await self.activate()
        with self.assertRaises(ModerationUnavailableError):
            await service.ask_text(**self.request())
        self.assertEqual(create.await_count, 2)
        self.assertEqual(await self.rows("SELECT * FROM user_warnings"), [])
        errors = await self.rows("SELECT status, mode FROM safety_evaluations WHERE mode = 'production'")
        self.assertEqual(errors, [{"status": "error", "mode": "production"}])

    async def test_one_format_retry_is_technical_and_does_not_seek_a_harsher_decision(self):
        service, create = self.adaptive_service([completion(content="bad JSON"), assessment_response(harmful(confidence=.7))])
        await self.activate()
        with self.assertRaises(ProviderModerationError) as caught:
            await service.ask_text(**self.request())
        self.assertEqual(caught.exception.result.effective_action, "refuse")
        self.assertEqual(create.await_count, 2)
        self.assertEqual((await self.accounts.status(2))["requests_today"], 0)
        record = (await self.rows("SELECT contract_retries, action FROM safety_evaluations WHERE mode = 'production'"))[0]
        self.assertEqual(record, {"contract_retries": 1, "action": "refuse"})
        self.assertEqual(await self.rows("SELECT * FROM user_warnings"), [])

    async def test_changed_fingerprint_cannot_silently_enforce_unreviewed_engine(self):
        service, create = self.adaptive_service([completion()])
        await self.activate()
        self.moderation.adaptive_moderator.model_key = "gpt_oss_safeguard_120b"
        with self.assertRaises(ModerationUnavailableError):
            await service.ask_text(**self.request())
        create.assert_not_awaited()
        self.assertEqual(await self.rows("SELECT * FROM user_warnings"), [])

    async def test_safe_completion_context_is_isolated_between_concurrent_users(self):
        await self.add_user(3)

        async def responses(**kwargs):
            if kwargs["model"] == "openai.gpt-oss-safeguard-20b":
                is_defensive = "defensive task" in kwargs["messages"][-1]["content"]
                value = facts(intent="defensive_security", authorization="authorized") if is_defensive else facts()
                await asyncio.sleep(.01)
                return assessment_response(value)
            request = kwargs["messages"][-1]["content"]
            constrained = "AIS SAFE COMPLETION REQUIREMENT" in kwargs["messages"][0]["content"]
            self.assertEqual(constrained, request == "defensive task")
            return completion()

        service, _ = self.adaptive_service(responses)
        await self.activate()
        await asyncio.gather(service.ask_text(**self.request(user_id=2, prompt="defensive task")),
                             service.ask_text(**self.request(user_id=3, prompt="ordinary task")))
        self.assertIsNone(safe_completion_mode.get())

    async def test_non_violation_result_cannot_be_sent_to_warning_repository(self):
        from AI.moderation import ModerationResult
        with self.assertRaises(ValueError):
            await self.moderation.confirm(user_id=2, request_id="wrong", model="m", provider="p",
                result=ModerationResult(True, "adaptive_policy_engine", policy_action="refuse"))
        self.assertEqual(await self.rows("SELECT * FROM user_warnings"), [])
