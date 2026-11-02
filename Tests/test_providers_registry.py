"""Regression: a second provider adapter must not bypass the single gateway."""
import unittest
from AI.providers import available_providers, model_for_backend
from Tests.test_routing import make_router, generation


class GatewayTests(unittest.IsolatedAsyncioTestCase):
    async def test_persisted_legacy_selector_resolves_to_gateway(self):
        self.assertEqual(available_providers(), (("amazon_bedrock", "☁️ AiS API"),))
        self.assertEqual(model_for_backend("amazon_bedrock", "gpt_oss_20b").backend_provider, "ais_api")
        router, state, client, _ = make_router()
        key = (31, 32)
        await router.ask_text(conversation_key=key, conversation_id=generation(state, key),
                              model_key="gpt_oss_20b", prompt="Hello")
        self.assertEqual(client.calls[0]["model"].model_id, "openai.gpt-oss-20b")
        self.assertIn("AiS API", client.calls[0]["system_prompt"])

    def test_unknown_provider_is_not_an_inference_route(self):
        with self.assertRaises(KeyError):
            model_for_backend("azure", "gpt_oss_20b")
