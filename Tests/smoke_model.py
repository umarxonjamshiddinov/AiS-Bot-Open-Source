"""Explicit, paid single-model smoke test. Never imported or run automatically."""

import argparse
import asyncio

from AI.client import AISClient
from AI.models import get_model
from AI.prompts import SYSTEM_PROMPT
from config import Settings


async def run(model_key: str) -> None:
    settings = Settings.load()
    model = get_model(model_key)
    if not model.user_visible:
        raise ValueError("Smoke model accepts public models only")
    client = AISClient(
        api_key=settings.ais_api_key,
        base_url=settings.ais_api_base_url,
        timeout=settings.ai_request_timeout_seconds,
        retry_count=settings.ai_retry_count,
    )
    answer = await client.complete(
        model=model,
        messages=[{"role": "user", "content": "Ответь только: OK"}],
        system_prompt=SYSTEM_PROMPT,
        max_tokens=16,
    )
    print("text_response_nonempty", bool(answer.strip()))
    await client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("model_key")
    args = parser.parse_args()
    asyncio.run(run(args.model_key))
