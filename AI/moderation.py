"""One moderation contract for every model/route.

Only typed provider metadata and explicit provider errors are evidence. Content,
refusal wording, HTTP 403 alone and user-authored JSON are never classifiers.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass


SAFETY_REFUSAL = "I’m sorry, but I can’t help with that."

_CODES = {
    "content_filter": "content_filter",
    "content_filtered": "content_filter",
    "content_policy_violation": "content_policy_violation",
    "responsibleai_policy_violation": "responsibleai_policy_violation",
    "responsibleaipolicyviolation": "responsibleai_policy_violation",
    "guardrail_intervened": "guardrail_intervened",
    "provider_refusal": "provider_refusal",
}


def machine_code(value) -> str | None:
    return value if isinstance(value, str) and re.fullmatch(r"[a-zA-Z0-9_.:-]{1,80}", value) else None


def _get(value, key, default=None):
    return value.get(key, default) if isinstance(value, dict) else getattr(value, key, default)


def _confidence(value) -> float | None:
    if type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1:
        return float(value)
    return None


@dataclass(frozen=True, slots=True)
class ModerationResult:
    blocked: bool
    source: str
    code: str | None = None
    provider_code: str | None = None
    category: str | None = None
    severity: str | None = None
    confidence: float | None = None
    stage: str = "provider"
    classifier_model: str | None = None
    classifier_provider: str | None = None
    policy_version: str | None = None
    intent: str | None = None
    confidence_source: str | None = None
    policy_action: str | None = None
    policy_fingerprint: str | None = None
    safety_evaluation_id: int | None = None
    safe_mode: str | None = None

    @property
    def allowed(self) -> bool:
        return not self.blocked

    @property
    def effective_action(self) -> str:
        return self.policy_action or ("violation" if self.blocked else "allow")


class ProviderModerationError(RuntimeError):
    def __init__(self, code: str = "provider_moderation", *, result: ModerationResult | None = None,
                 model: str | None = None, provider: str | None = None) -> None:
        self.result = result or ModerationResult(
            True, "provider_error", _CODES.get(code.lower(), "provider_moderation"), machine_code(code))
        if not self.result.blocked:
            raise ValueError("A moderation error requires a blocked decision")
        self.code = self.result.code or "provider_moderation"
        self.model, self.provider = model, provider
        self.outcome = None
        # Exception text is safe even if an operational logger prints it.
        super().__init__(self.code)


def _blocked(source: str, code: str, metadata=None, provider_code=None) -> ModerationResult:
    return ModerationResult(
        True, source, code, machine_code(provider_code),
        machine_code(_get(metadata, "category")), machine_code(_get(metadata, "severity")),
        _confidence(_get(metadata, "confidence")),
    )


def _filters(metadata) -> ModerationResult | None:
    if not isinstance(metadata, dict):
        return None
    for category, details in metadata.items():
        if isinstance(details, dict) and details.get("filtered") is True:
            return _blocked("provider_metadata", "content_filter",
                            {**details, "category": category}, "content_filter")
    return None


def _metadata(value) -> ModerationResult | None:
    moderation = _get(value, "moderation")
    if isinstance(moderation, dict) and (
        moderation.get("blocked") is True or moderation.get("decision") in ("block", "blocked")
        or moderation.get("action") == "block" or moderation.get("status") == "blocked"
    ):
        supplied_code = machine_code(moderation.get("code"))
        return _blocked("provider_metadata", _CODES.get((supplied_code or "").lower(), "provider_moderation"),
                        moderation, supplied_code)
    filtered = _filters(_get(value, "content_filter_results")) or _filters(_get(value, "content_filter_result"))
    if filtered:
        return filtered
    for field in ("stopReason", "stop_reason"):
        raw = _get(value, field)
        if isinstance(raw, str) and raw.lower() in _CODES:
            return _blocked("provider_metadata", _CODES[raw.lower()], provider_code=raw)
    return None


def moderation_from_response(response) -> ModerationResult:
    decision = _metadata(response) or _metadata(_get(response, "metadata"))
    prompt_results = _get(response, "prompt_filter_results", [])
    if not decision and isinstance(prompt_results, list):
        decision = next((found for entry in prompt_results if (found := _metadata(entry))), None)
    if decision:
        return decision
    for choice in _get(response, "choices", []) or []:
        decision = _metadata(choice) or _metadata(_get(choice, "metadata"))
        if decision:
            return decision
        finish = _get(choice, "finish_reason")
        if isinstance(finish, str) and finish.lower() in _CODES:
            return _blocked("provider_finish_reason", _CODES[finish.lower()], provider_code=finish)
        # A dedicated refusal field is a signal; its text is deliberately ignored.
        refusal = _get(_get(choice, "message"), "refusal")
        if isinstance(refusal, str) and refusal.strip():
            return _blocked("provider_refusal", "provider_refusal")
    # This is permission to continue, not a fabricated classifier verdict. No
    # category, confidence or "safe" event is manufactured from missing metadata.
    return ModerationResult(False, "no_block_signal")


def moderation_from_error(error: BaseException) -> ModerationResult | None:
    if isinstance(error, ProviderModerationError):
        return error.result
    body = getattr(error, "body", None)
    detail = body.get("error", body) if isinstance(body, dict) else None
    inner = _get(detail, "innererror") or _get(detail, "inner_error")
    for value in (inner, detail, error):
        result = _metadata(value)
        if result:
            return result
    for value in (inner, detail, error):
        raw_code = _get(value, "code")
        if isinstance(raw_code, str) and raw_code.lower() in _CODES:
            return _blocked("provider_error", _CODES[raw_code.lower()], value, raw_code)
    return None
