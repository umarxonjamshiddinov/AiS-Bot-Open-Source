"""Exercise the actual SDK JSON parser, not an AsyncMock of post()."""
import asyncio
import json
import unittest

import httpx2
from openai import AsyncOpenAI

from AI.client import AISClient
from AI.input_moderation import InputModerator
from AI.safety_client import RemoteSafetyClient, safety_receipt


class SafetySDKTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_sdk_decodes_structured_safety_without_provider_calls(self):
        requests = []
        def respond(request):
            requests.append(json.loads(request.content))
            return httpx2.Response(200, json={"policy_version": "ais_safety_v1", "action": "allow",
                "decision": {"version": "ais_safety_v1", "decision": "allowed", "category": None},
                "receipt": "test-receipt", "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5}})
        gateway = AISClient(api_key="fake-only", base_url="https://ais.example/v1", timeout=5, retry_count=0)
        await gateway._client.close()
        gateway._client = AsyncOpenAI(api_key="fake-only", base_url="https://ais.example/v1", max_retries=0,
            http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(respond)))
        token = safety_receipt.set(None)
        try:
            result = await InputModerator(RemoteSafetyClient(gateway=gateway), asyncio.Semaphore(1)).check(prompt="Hello")
            self.assertTrue(result.allowed)
            self.assertEqual(safety_receipt.get(), "test-receipt")
            self.assertEqual(len(requests), 1)
            self.assertNotIn("system_prompt", requests[0])
        finally:
            safety_receipt.reset(token)
            await gateway.close()
