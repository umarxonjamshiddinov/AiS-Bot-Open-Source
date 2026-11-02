from __future__ import annotations

import asyncio
import base64
import logging
import time
from collections.abc import Sequence
from urllib.parse import urlsplit

import httpx
from openai import AsyncOpenAI, DefaultAsyncHttpxClient

from AI.models import ModelInfo, user_models
from AI.moderation import moderation_from_error, moderation_from_response
from AI.telemetry import (
    ProviderModerationError,
    TokenUsage,
    attempt_observer,
    inference_was_started,
)
from Utils.privacy import known_secrets

logger = logging.getLogger(__name__)


class EmptyModelResponseError(RuntimeError):
    pass


def ais_base_url(value: str) -> str:
    parsed = urlsplit(value.rstrip("/"))
    if (parsed.scheme not in {"https", "http"} or not parsed.hostname
            or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise ValueError("AIS_API_BASE_URL must be an HTTP(S) URL without credentials, query or fragment")
    base = value.rstrip("/")
    return base if parsed.path.endswith("/v1") else base + "/v1"


class AISAPIError(RuntimeError):
    """Only fixed operational metadata; never retain SDK requests or response bodies."""

    def __init__(self, *, status_code: int | None = None, kind: str = "service", inference_started: bool = True):
        self.status_code = status_code
        self.kind = kind
        self.inference_started = inference_started
        super().__init__(f"AiS API failure ({kind}; status={status_code})")


class AISClient:
    """The only public-model transport. Upstream routing and retries belong to AiS API."""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        timeout: float,
        retry_count: int,
    ) -> None:
        for name in ("openai", "httpx", "httpcore", "httpx2", "httpcore2"):
            logging.getLogger(name).setLevel(logging.CRITICAL + 1)
        self._redactions = known_secrets(api_key)
        self._client = AsyncOpenAI(
            api_key=api_key,
            base_url=ais_base_url(base_url),
            timeout=timeout,
            max_retries=0,
            http_client=DefaultAsyncHttpxClient(follow_redirects=False),
        )
        # Retain the setting during migration, but never replay an ambiguous paid call.
        # Even 429/5xx are final here: the gateway owns upstream retries.
        self._retry_count = 0

    def client_for(self, model: ModelInfo):
        return self._client

    async def validate_catalog(self) -> None:
        try:
            listing = await self._client.models.list()
            actual = {model.id for model in listing.data}
        except Exception as error:
            raise self._safe_error(error) from None
        expected = {model.model_id for model in user_models()}
        if actual != expected:
            raise AISAPIError(kind="catalog_mismatch", inference_started=False)

    async def complete(
        self,
        *,
        model: ModelInfo,
        messages: Sequence[dict],
        system_prompt: str,
        max_tokens: int,
        image_bytes: bytes | None = None,
        mime_type: str = "image/jpeg",
    ) -> str:
        request_messages = [{"role": "system", "content": system_prompt}]
        request_messages.extend(dict(message) for message in messages)

        if image_bytes is not None:
            request_messages = self._attach_image(
                request_messages,
                image_bytes=image_bytes,
                mime_type=mime_type,
            )

        request_client = self.client_for(model)

        for attempt in range(self._retry_count + 1):
            observer = attempt_observer.get()
            attempt_id = await observer.start(model) if observer else None
            started = time.perf_counter()
            usage = TokenUsage()
            status = "error"
            observed_error: BaseException | None = None
            try:
                from AI.safety_client import safety_receipt
                receipt = safety_receipt.get()
                extra = {"extra_body": {"safety_receipt": receipt}} if receipt else {}
                response = await request_client.chat.completions.create(
                    model=model.model_id,
                    messages=request_messages,
                    max_tokens=max_tokens,
                    **extra,
                )
                safety_receipt.set(None)

                usage = TokenUsage.from_response(response)
                moderation = moderation_from_response(response)
                if moderation.blocked:
                    raise ProviderModerationError(result=moderation, model=model.key, provider=model.backend_provider)
                choice = response.choices[0] if response.choices else None
                content = choice.message.content if choice is not None else None

                if not content:
                    raise EmptyModelResponseError(
                        "The model returned no text response"
                    )

                status = "succeeded"
                for secret in self._redactions:
                    content = content.replace(secret, "[redacted]")
                return content

            except asyncio.CancelledError as error:
                status = "cancelled"
                observed_error = error
                raise
            except Exception as error:
                observed_error = error
                moderation = moderation_from_error(error)
                if moderation:
                    status = "blocked"
                    if isinstance(error, ProviderModerationError):
                        error.model, error.provider = model.key, model.backend_provider
                        raise
                    observed_error = ProviderModerationError(result=moderation, model=model.key, provider=model.backend_provider)
                    raise observed_error from None
                if isinstance(error, (ValueError, TypeError)):
                    # SDK validation errors can include request values.
                    observed_error = ValueError("Inference request validation failed")
                    raise observed_error from None
                observed_error = self._safe_error(error)
                raise observed_error from None
            finally:
                if observer is not None and attempt_id is not None:
                    # Persistence failures must never retry a successful paid call.
                    await observer.finish(
                        attempt_id=attempt_id, model=model, status=status,
                        latency_ms=round((time.perf_counter() - started) * 1000),
                        usage=usage, error=observed_error,
                        inference_started=status != "blocked" and inference_was_started(observed_error),
                    )

        raise AssertionError("Unreachable code")

    async def close(self) -> None:
        await self._client.close()

    @staticmethod
    def _attach_image(
        messages: list[dict],
        *,
        image_bytes: bytes,
        mime_type: str,
    ) -> list[dict]:
        encoded = base64.b64encode(image_bytes).decode("ascii")
        data_url = f"data:{mime_type};base64,{encoded}"

        for index in range(len(messages) - 1, -1, -1):
            message = messages[index]

            if message.get("role") != "user":
                continue

            content = message.get("content")
            parts = list(content) if isinstance(content, list) else [
                {"type": "text", "text": content or "Analyze the image."}
            ]

            messages[index] = {
                **message,
                "content": [
                    *parts,
                    {
                        "type": "image_url",
                        "image_url": {"url": data_url},
                    },
                ],
            }

            return messages

        raise ValueError("An image requires a user message")

    @staticmethod
    def _safe_error(error: Exception) -> AISAPIError:
        if isinstance(error, AISAPIError):
            return error
        status = getattr(error, "status_code", None)
        status = status if type(status) is int and 100 <= status <= 599 else None
        cause = getattr(error, "__cause__", None)
        before_send = isinstance(cause, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout))
        kind = "timeout" if "Timeout" in type(error).__name__ else "connection" if "Connection" in type(error).__name__ else "service"
        return AISAPIError(status_code=status, kind=kind,
                           inference_started=not before_send and status not in {400, 401, 403, 404, 422, 429})
