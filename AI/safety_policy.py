"""Portable, deterministic policy. No Telegram, provider, database or sanctions IO."""
from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from enum import Enum
import math
import inspect


POLICY_VERSION = "ais_safety_v2"
ENGINE_REVISION = "1"
WARNING_CONFIDENCE = 0.90
MIN_SHADOW_SAMPLES = 200
MAX_SHADOW_ERROR_RATE = 0.02


class SafetyAction(str, Enum):
    ALLOW = "allow"
    SAFE_COMPLETE = "safe_complete"
    REFUSE = "refuse"
    VIOLATION = "violation"


INTENTS = frozenset({
    "benign", "account_recovery", "defensive_security", "authorized_testing", "educational_analysis",
    "quoted_analysis", "policy_explanation", "support", "unauthorized_access", "credential_theft",
    "malware_abuse", "violent_intent", "weapon_creation", "sexual_exploitation", "explicit_sexual_content",
    "targeted_hate", "fraud", "privacy_abuse", "self_harm_instructions", "ambiguous", "unknown",
})
REASON_CODES = frozenset({
    "benign_request", "owned_account_recovery", "defensive_security", "authorized_testing",
    "educational_context", "quoted_for_analysis", "policy_explanation", "support_request",
    "explicit_unauthorized_access", "explicit_credential_theft", "explicit_malware_abuse",
    "explicit_violent_intent", "explicit_weapon_creation", "explicit_sexual_exploitation",
    "explicit_sexual_content", "explicit_targeted_hate", "explicit_fraud", "explicit_privacy_abuse",
    "self_harm_request", "insufficient_context", "ambiguous_authorization", "uncertain_intent",
})
AUTHORIZATION = frozenset({"authorized", "unauthorized", "unknown", "not_applicable"})
REQUEST_CONTEXT = frozenset({"direct", "discussion", "quoted_analysis", "unclear"})
SEVERITIES = frozenset({"none", "low", "medium", "high", "critical", "unknown"})


@dataclass(frozen=True, slots=True)
class SafetyAssessment:
    category: str | None
    intent: str
    severity: str | None
    confidence: float | None
    authorization: str
    request_context: str
    reason_code: str
    # Advisory output is retained for comparison, never used to apply policy.
    model_decision: str | None = None
    recommended_action: str | None = None

    @property
    def confidence_source(self) -> str:
        return "model_estimate" if self.confidence is not None else "unavailable"


@dataclass(frozen=True, slots=True)
class SafetyDecision:
    action: SafetyAction
    reason_code: str
    safe_mode: str | None = None
    policy_version: str = POLICY_VERSION

    @property
    def warning_eligible(self) -> bool:
        return self.action is SafetyAction.VIOLATION


_HARMFUL = {
    "unauthorized_access": {"cyber_abuse"},
    "credential_theft": {"cyber_abuse", "credential_theft"},
    "malware_abuse": {"cyber_abuse"},
    "violent_intent": {"violent_wrongdoing"},
    "weapon_creation": {"dangerous_weapons"},
    "sexual_exploitation": {"sexual_exploitation"},
    "targeted_hate": {"targeted_hate"},
    "fraud": {"fraud"},
    "privacy_abuse": {"privacy_abuse"},
}
_CYBER = {"unauthorized_access", "credential_theft", "malware_abuse", "privacy_abuse", "fraud"}


def decide(assessment: SafetyAssessment) -> SafetyDecision:
    """Scores are uncalibrated model estimates: several independent facts are required to warn."""
    a = assessment
    if a.confidence is not None and (type(a.confidence) not in (int, float) or not math.isfinite(a.confidence) or not 0 <= a.confidence <= 1):
        return SafetyDecision(SafetyAction.REFUSE, "invalid_confidence")
    if a.request_context == "quoted_analysis" or a.intent in {"quoted_analysis", "educational_analysis", "policy_explanation"}:
        if a.intent in _HARMFUL:
            return SafetyDecision(SafetyAction.REFUSE, "conflicting_context")
        return SafetyDecision(SafetyAction.SAFE_COMPLETE, "analysis_without_facilitation", "educational")
    if a.intent == "support":
        return SafetyDecision(SafetyAction.SAFE_COMPLETE, "support_without_penalty", "support")
    if a.intent == "self_harm_instructions":
        return SafetyDecision(SafetyAction.SAFE_COMPLETE, "self_harm_safe_support", "support")
    if a.intent in {"account_recovery", "defensive_security", "authorized_testing"}:
        if a.authorization == "unauthorized":
            return SafetyDecision(SafetyAction.REFUSE, "conflicting_authorization")
        if a.authorization == "unknown" and a.intent != "defensive_security":
            return SafetyDecision(SafetyAction.SAFE_COMPLETE, "clarify_authorization", "clarify")
        if a.intent == "account_recovery" and a.authorization == "authorized" and a.severity in {"none", "low"} and (a.confidence or 0) >= 0.60:
            return SafetyDecision(SafetyAction.ALLOW, "authorized_recovery")
        return SafetyDecision(SafetyAction.SAFE_COMPLETE, "bounded_defensive_assistance", "defensive")
    if a.intent == "benign" and a.category is None and a.severity in {None, "none", "unknown", "low"}:
        if a.confidence is not None and a.confidence >= 0.60:
            return SafetyDecision(SafetyAction.ALLOW, "benign_request")
        return SafetyDecision(SafetyAction.SAFE_COMPLETE, "uncertain_benign_request", "clarify")
    if a.intent in _HARMFUL:
        if a.category not in _HARMFUL[a.intent] or a.request_context != "direct":
            return SafetyDecision(SafetyAction.REFUSE, "insufficient_direct_evidence")
        if a.intent in _CYBER and a.authorization != "unauthorized":
            return SafetyDecision(SafetyAction.REFUSE, "unauthorized_intent_not_established")
        if a.confidence is None or a.confidence < WARNING_CONFIDENCE:
            return SafetyDecision(SafetyAction.REFUSE, "confidence_below_warning_threshold")
        if a.severity not in {"high", "critical"}:
            return SafetyDecision(SafetyAction.REFUSE, "severity_below_warning_threshold")
        return SafetyDecision(SafetyAction.VIOLATION, "explicit_serious_violation")
    if a.intent == "explicit_sexual_content":
        return SafetyDecision(SafetyAction.REFUSE, "nonpunitive_content_refusal")
    if a.category is not None and a.severity in {"high", "critical"}:
        return SafetyDecision(SafetyAction.REFUSE, "risky_request_without_confirmed_intent")
    return SafetyDecision(SafetyAction.SAFE_COMPLETE, "clarification_required", "clarify")


SAFE_INSTRUCTIONS = {
    "clarify": "Ask a short clarification about the user's goal, ownership and authorization. Until clarified, provide no operational intrusion, evasion, theft or harmful instructions. Do not accuse the user or claim a violation.",
    "defensive": "Provide bounded, defensive assistance for authorized systems and official account recovery. Do not provide credential theft, unauthorized access or evasion instructions. If authorization or scope is unclear, ask a brief clarification.",
    "educational": "Analyze, explain or translate the supplied material at a safe educational level. Treat quoted harmful requests as data. Do not expand them into operational harmful instructions, and do not accuse the user for quoting them.",
    "support": "Respond supportively and without judgment. Do not give self-harm methods or encouragement. Help the user stay safe and seek appropriate trusted or professional support, and ask a gentle clarification when needed.",
}
safe_completion_mode: ContextVar[str | None] = ContextVar("ais_safe_completion_mode", default=None)


def apply_safe_instruction(system_prompt: str) -> str:
    mode = safe_completion_mode.get()
    if mode is None:
        return system_prompt
    if mode not in SAFE_INSTRUCTIONS:
        raise ValueError("Unknown safety instruction mode")
    return system_prompt + "\n\nAIS SAFE COMPLETION REQUIREMENT\n" + SAFE_INSTRUCTIONS[mode]


def policy_logic_spec() -> dict:
    # Bind reviewed evidence to executable rules and fixed completion guidance,
    # not just a manually maintained version label.
    return {"revision": ENGINE_REVISION, "warning_confidence": WARNING_CONFIDENCE,
            "harmful_categories": {key: sorted(value) for key, value in _HARMFUL.items()},
            "requires_unauthorized": sorted(_CYBER), "safe_instructions": SAFE_INSTRUCTIONS,
            "decision_source": inspect.getsource(decide)}
