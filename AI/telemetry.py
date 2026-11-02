"""Optional request-scoped observation; no database or Telegram dependencies."""
from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from typing import Protocol
import httpx

from AI.moderation import ProviderModerationError, moderation_from_error


@dataclass(frozen=True, slots=True)
class TokenUsage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None

    @classmethod
    def from_response(cls, response) -> "TokenUsage":
        usage = getattr(response, "usage", None)

        def number(name):
            value = usage.get(name) if isinstance(usage, dict) else getattr(usage, name, None)
            return value if type(value) is int and value >= 0 else None

        # Preserve absent data. Never estimate from text length or invent totals.
        return cls(number("prompt_tokens"), number("completion_tokens"), number("total_tokens"))


def moderation_error_code(error: BaseException) -> str | None:
    result = moderation_from_error(error)
    return result.code if result else None


def safe_error_code(error: BaseException) -> str | None:
    code = moderation_error_code(error)
    if code:
        return code
    status = getattr(error, "status_code", None)
    if type(status) is int and 100 <= status <= 599:
        return f"http_{status}"
    return None  # Never retain messages, request bodies, headers, or arbitrary codes.


def inference_was_started(error: BaseException | None) -> bool:
    """Refund only failures known to precede inference; timeouts after send are ambiguous."""
    if hasattr(error, "inference_started"):
        return error.inference_started
    if isinstance(error, (ValueError, TypeError)):
        return False
    if getattr(error, "status_code", None) in {400, 401, 403, 404, 405, 409, 413, 415, 422, 429}:
        return False
    cause = getattr(error, "__cause__", None)
    return not isinstance(cause, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.InvalidURL))


class AttemptObserver(Protocol):
    async def start(self, model) -> str: ...
    async def finish(self, *, attempt_id: str, model, status: str, latency_ms: int,
                     usage: TokenUsage, error: BaseException | None, inference_started: bool = True) -> None: ...


attempt_observer: ContextVar[AttemptObserver | None] = ContextVar("ai_attempt_observer", default=None)
attempt_purpose: ContextVar[str] = ContextVar("ai_attempt_purpose", default="product")
main_inference: ContextVar[bool] = ContextVar("ai_main_inference", default=True)
