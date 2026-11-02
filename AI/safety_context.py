"""Bounded context builder. Current requests are never silently truncated."""
from dataclasses import dataclass
import json

from AI.input_moderation import ModerationUnavailableError


@dataclass(frozen=True, slots=True)
class SafetyInput:
    user_request: str
    material: str | None = None
    kind: str = "text"


def build_safety_context(prompt: str, history=(), recent_refusals=(), safety_input: SafetyInput | None = None) -> dict:
    current = safety_input or SafetyInput(prompt)
    if current.kind not in {"text", "document", "image"} or not isinstance(current.user_request, str) or not current.user_request.strip():
        raise ModerationUnavailableError("Invalid safety input")
    if len(prompt.encode("utf-8")) > 96_000:
        raise ModerationUnavailableError("Input is too large for safety context")
    turns, size, truncated = [], 0, False
    for message in reversed(list(history)[-8:]):
        if message.get("role") not in {"user", "assistant"} or not isinstance(message.get("content"), str):
            continue
        item = {"role": message["role"], "content": message["content"]}
        length = len(json.dumps(item, ensure_ascii=False).encode("utf-8"))
        if size + length > 12_000:
            truncated = True
            break
        turns.append(item)
        size += length
    context = {
        "current_request": {"kind": current.kind, "user_request": current.user_request, "untrusted_material": current.material},
        "recent_conversation": list(reversed(turns)),
        "prior_user_request": next((item["content"] for item in turns if item["role"] == "user"), None),
        "previous_refused_requests": list(recent_refusals)[-3:],
        "history_truncated": truncated or len(history) > 8,
    }
    while len(json.dumps(context, ensure_ascii=False).encode("utf-8")) > 112_000:
        if context["recent_conversation"]:
            context["recent_conversation"].pop(0)
            context["prior_user_request"] = next((item["content"] for item in reversed(context["recent_conversation"]) if item["role"] == "user"), None)
        elif context["previous_refused_requests"]:
            context["previous_refused_requests"].pop(0)
        else:
            raise ModerationUnavailableError("Input is too large for safety context")
        context["history_truncated"] = True
    return context
