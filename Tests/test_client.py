import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from openai import APIConnectionError, APIStatusError

from AI.client import AISAPIError, AISClient, ais_base_url
from AI.errors import inference_error_message
from AI.models import get_model, user_models
from AI.telemetry import inference_was_started


def completion(text="ok"):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text))])


class AISClientTests(unittest.IsolatedAsyncioTestCase):
    def client(self):
        fake = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=AsyncMock(return_value=completion()))),
                               models=SimpleNamespace(list=AsyncMock()), close=AsyncMock())
        with patch("AI.client.AsyncOpenAI", return_value=fake) as factory:
            client = AISClient(api_key="test-secret", base_url="https://ais.example", timeout=10, retry_count=2)
        self.assertEqual(factory.call_args.kwargs["max_retries"], 0)
        self.assertEqual(factory.call_args.kwargs["base_url"], "https://ais.example/v1")
        return client, fake

    async def test_all_40_models_use_one_gateway_with_exact_ids(self):
        client, fake = self.client()
        for model in user_models():
            await client.complete(model=model, messages=[{"role": "developer", "content": "keep"},
                {"role": "user", "content": "hello"}], system_prompt="system", max_tokens=128)
        self.assertEqual([c.kwargs["model"] for c in fake.chat.completions.create.call_args_list],
                         [m.model_id for m in user_models()])
        self.assertEqual(fake.chat.completions.create.call_args.kwargs["messages"][1]["role"], "developer")
        await client.close()

    async def test_image_preserves_existing_parts_history_and_name(self):
        client, fake = self.client()
        messages = [{"role": "assistant", "content": "old"}, {"role": "user", "name": "tester",
                     "content": [{"type": "text", "text": "look"}]}]
        await client.complete(model=get_model("deepseek_v3_1"), messages=messages, system_prompt="system",
                              max_tokens=128, image_bytes=b"image", mime_type="image/png")
        sent = fake.chat.completions.create.call_args.kwargs["messages"]
        self.assertEqual(sent[-1]["name"], "tester")
        self.assertEqual(sent[-1]["content"][0]["text"], "look")
        self.assertTrue(sent[-1]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,"))
        self.assertEqual(len(messages[-1]["content"]), 1)

    async def test_http_errors_are_sanitized_and_never_retried(self):
        for status in (400, 401, 403, 404, 429, 500, 502, 503, 504):
            client, fake = self.client()
            request = httpx.Request("POST", "https://sensitive.example", headers={"Authorization": "Bearer test-secret"})
            fake.chat.completions.create.side_effect = APIStatusError("test-secret raw exception",
                response=httpx.Response(status, request=request), body={"error": {"message": "test-secret"}})
            with self.assertRaises(AISAPIError) as caught:
                await client.complete(model=get_model("gpt_oss_20b"), messages=[], system_prompt="s", max_tokens=1)
            error = caught.exception
            self.assertEqual(error.status_code, status)
            self.assertNotIn("test-secret", str(error))
            self.assertNotIn("sensitive.example", str(error))
            self.assertIsNone(error.__cause__)
            self.assertFalse(hasattr(error, "body"))
            self.assertNotIn("test-secret", inference_error_message(error))
            fake.chat.completions.create.assert_awaited_once()

    async def test_connection_failure_keeps_refund_semantics_without_sdk_request(self):
        client, fake = self.client()
        error = APIConnectionError(request=httpx.Request("POST", "https://ais.example"))
        error.__cause__ = httpx.ConnectError("sensitive")
        fake.chat.completions.create.side_effect = error
        with self.assertRaises(AISAPIError) as caught:
            await client.complete(model=get_model("gpt_oss_20b"), messages=[], system_prompt="s", max_tokens=1)
        self.assertFalse(inference_was_started(caught.exception))

    async def test_catalog_validation_and_internal_exclusion(self):
        client, fake = self.client()
        fake.models.list.return_value = SimpleNamespace(data=[SimpleNamespace(id=m.model_id) for m in user_models()])
        await client.validate_catalog()
        fake.models.list.return_value.data.append(SimpleNamespace(id="openai.gpt-oss-safeguard-20b"))
        with self.assertRaises(AISAPIError):
            await client.validate_catalog()

    async def test_reflected_secret_is_redacted(self):
        client, fake = self.client()
        fake.chat.completions.create.return_value = completion("answer test-secret")
        result = await client.complete(model=get_model("gpt_oss_20b"), messages=[], system_prompt="s", max_tokens=1)
        self.assertEqual(result, "answer [redacted]")

    def test_base_url_validation(self):
        self.assertEqual(ais_base_url("https://ais.example/v1/"), "https://ais.example/v1")
        for value in ("https://key:secret@ais.example", "https://ais.example?key=secret", "bad"):
            with self.assertRaises(ValueError):
                ais_base_url(value)
