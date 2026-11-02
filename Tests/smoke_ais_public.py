"""Real public API smoke independent of the temporary legacy Safety configuration."""
import asyncio
import json
import os

from dotenv import load_dotenv
from openai import APIStatusError

from AI.client import AISClient, AISAPIError
from AI.errors import inference_error_message
from AI.input_moderation import InputModerator
from AI.router import AIRouter
from Tests.smoke_moderation import synthetic_image
from Tests.test_routing import FakeSearch
from config import PROJECT_ROOT
from state import BotState


async def run():
    load_dotenv(PROJECT_ROOT / ".env")
    client = AISClient(api_key=os.environ["AIS_API_KEY"], base_url=os.environ["AIS_API_BASE_URL"],
                       timeout=120, retry_count=0)
    checks = []

    def record(case, passed, **metadata):
        checks.append(bool(passed))
        print(json.dumps({"case": case, "passed": bool(passed), **metadata}), flush=True)

    state = BotState()
    router = AIRouter(client=client, search=FakeSearch(), state=state,
                      max_concurrent_requests=2, default_max_output_tokens=128)
    try:
        await client.validate_catalog()
        record("models", True, public_models=40)
        for key in ("gpt_oss_20b", "gemma_3_4b_it", "grok_4_6"):
            try:
                conversation = (1, 2)
                result = await router.ask_text(conversation_key=conversation,
                    conversation_id=state.current_conversation_id(conversation), model_key=key,
                    prompt="Reply briefly with OK.")
                record("text_" + key, bool(result.text.strip()) and len(state.histories[conversation]) == 2)
                state.reset_conversation(*conversation)
            except AISAPIError as error:
                record("text_" + key, False, status=error.status_code, kind=error.kind)
        image = synthetic_image()
        try:
            verdict = await InputModerator(client, asyncio.Semaphore(1)).check(
                prompt="Describe the solid background color.", image_bytes=image, mime_type="image/png")
            record("image_safety_via_ais", verdict.allowed)
        except Exception as error:
            record("image_safety_via_ais", False, error_type=type(error).__name__)
        for key in ("gemma_3_4b_it", "gpt_oss_20b"):
            try:
                conversation = (3, 4)
                result = await router.ask_image(conversation_key=conversation,
                    conversation_id=state.current_conversation_id(conversation), model_key=key,
                    prompt="What is the solid background color? Reply in one sentence.",
                    image_bytes=image, mime_type="image/png")
                record("vision_" + key, bool(result.text.strip()) and result.inference_count == 1)
                state.reset_conversation(*conversation)
            except AISAPIError as error:
                record("vision_" + key, False, status=error.status_code, kind=error.kind)
        for case, payload, expected in (
            ("unknown_model", {"model": "ais-smoke-nonexistent", "messages": [{"role": "user", "content": "test"}]}, {403, 404}),
            ("invalid_request", {"model": "openai.gpt-oss-20b", "messages": []}, {400, 422}),
        ):
            try:
                await client._client.post("chat/completions", body=payload, cast_to=dict)
                record(case, False)
            except APIStatusError as raw:
                error = client._safe_error(raw)
                record(case, error.status_code in expected and bool(inference_error_message(error)), status=error.status_code)
        response = await client._client._client.get(str(client._client.base_url) + "models",
            headers={"Authorization": "Bearer invalid-smoke-key"})
        record("invalid_auth", response.status_code == 401, status=response.status_code)
    finally:
        await client.close()
    print(json.dumps({"passed": sum(checks), "total": len(checks), "real_telegram_messages": 0}))
    return int(not all(checks))


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(run()))
    except Exception as error:
        print(json.dumps({"fatal_type": type(error).__name__, "status": getattr(error, "status_code", None)}))
        raise SystemExit(1) from None
