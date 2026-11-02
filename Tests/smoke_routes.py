"""Explicit paid route audit. Never imported or run by the test suite."""

import argparse
import asyncio

from AI.client import AISClient
from AI.models import MODEL_REGISTRY, ModelInfo, user_models
from config import Settings


def classify_error(error: Exception) -> str:
    status = getattr(error, "status_code", None)
    detail = str(error).lower()
    if status in {401, 403}:
        return "authentication_or_access"
    if "isn't supported on this route" in detail or "not supported on this route" in detail:
        return "unsupported_route"
    if "model" in detail and "not found" in detail:
        return "invalid_or_unavailable_model_id"
    if status == 400:
        return "validation_error"
    if status == 429:
        return "rate_limit"
    if isinstance(status, int) and status >= 500:
        return "temporary_service_error"
    return type(error).__name__


async def probe(
    client: AISClient,
    model: ModelInfo,
    semaphore: asyncio.Semaphore,
    max_tokens: int,
) -> dict[str, str | int | None]:
    try:
        async with semaphore:
            content = await client.complete(
                model=model, system_prompt="Reply concisely.",
                messages=[{"role": "user", "content": "Reply only with: OK"}],
                max_tokens=max_tokens,
            )
        return {
            "key": model.key,
            "model_id": model.model_id,
            "status": 200,
            "result": "ok" if content.strip() else "http_200_empty_output",
            "finish_reason": None,
            "error": None,
        }
    except Exception as error:
        return {
            "key": model.key,
            "model_id": model.model_id,
            "status": getattr(error, "status_code", None),
            "result": "error",
            "finish_reason": None,
            "error": classify_error(error),
        }


async def run(keys: list[str], concurrency: int, max_tokens: int) -> None:
    settings = Settings.load()
    models = [MODEL_REGISTRY[key] for key in keys] if keys else list(user_models())
    if any(not m.user_visible for m in models):
        raise ValueError("Smoke routes accepts public models only")
    client = AISClient(
        api_key=settings.ais_api_key,
        base_url=settings.ais_api_base_url,
        timeout=settings.ai_request_timeout_seconds,
        retry_count=0,
    )
    semaphore = asyncio.Semaphore(concurrency)
    results = await asyncio.gather(
        *(probe(client, model, semaphore, max_tokens) for model in models)
    )
    await client.close()
    for result in results:
        fields = " ".join(f"{key}={value}" for key, value in result.items())
        print(fields)
    passed = sum(result["status"] == 200 for result in results)
    print(f"SUMMARY checked={len(results)} http_200={passed} failed={len(results) - passed}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("keys", nargs="*")
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--max-tokens", type=int, default=16)
    args = parser.parse_args()
    asyncio.run(run(args.keys, args.concurrency, args.max_tokens))
