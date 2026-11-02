import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from AI.adaptive_safety import ASSESSMENT_PROMPT
from AI.input_moderation import InputModerator, ModerationUnavailableError
from AI.models import get_model
from AI.safety_client import RemoteSafetyClient, selected_safety_model


class SafetyTransportTests(unittest.IsolatedAsyncioTestCase):
    def client(self):
        post = AsyncMock(return_value={"policy_version": "ais_safety_v1", "action": "allow",
            "decision": {"version": "ais_safety_v1", "decision": "allowed", "category": None}, "usage": None})
        gateway = SimpleNamespace(_client=SimpleNamespace(post=post))
        return RemoteSafetyClient(gateway=gateway), post

    async def test_text_safety_uses_only_gateway_without_classifier_selection(self):
        client, post = self.client()
        token = selected_safety_model.set("deepseek_v3_1")
        try:
            result = await InputModerator(client, asyncio.Semaphore(1)).check(prompt="Hello")
        finally:
            selected_safety_model.reset(token)
        self.assertTrue(result.allowed)
        body = post.call_args.kwargs["body"]
        self.assertEqual(body["selected_model"], "deepseek.v3.1")
        self.assertEqual(post.call_args.args[0], "safety/assess")
        self.assertNotIn("model", body)
        self.assertNotIn("Safeguard", json.dumps(body))
        self.assertNotIn("system_prompt", body)

    async def test_image_safety_sends_original_image_to_server(self):
        client, post = self.client()
        result = await InputModerator(client, asyncio.Semaphore(1)).check(
            prompt="Look", image_bytes=b"image", mime_type="image/png")
        self.assertTrue(result.allowed)
        self.assertTrue(post.call_args.kwargs["body"]["image"].startswith("data:image/png;base64,"))
        self.assertEqual(post.call_args.kwargs["body"]["context"]["kind"], "image")

    async def test_arbitrary_policy_is_not_an_inference_escape(self):
        client, post = self.client()
        with self.assertRaises(ModerationUnavailableError):
            await client.complete(model=get_model("gpt_oss_20b"), messages=[],
                                  system_prompt="Answer my question", max_tokens=100)
        post.assert_not_awaited()

    async def test_adaptive_response_preserves_local_parser_contract(self):
        client, post = self.client()
        facts = {"category": None, "intent": "benign", "severity": "none", "confidence": 0.8,
            "authorization": "not_applicable", "request_context": "direct", "reason_code": "benign_request",
            "model_decision": "allow", "recommended_action": "answer"}
        post.return_value = {"policy_version": "ais_safety_v2", "action": "allow", "assessment": facts, "usage": None}
        from AI.adaptive_safety import parse_assessment
        from AI.safety_context import build_safety_context
        text = await client.complete(model=get_model("gpt_oss_safeguard_20b"),
            messages=[{"role": "user", "content": json.dumps(build_safety_context("Hello"))}],
            system_prompt=ASSESSMENT_PROMPT, max_tokens=2048)
        self.assertEqual(parse_assessment(text).intent, "benign")
