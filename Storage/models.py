from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone


def utc_timestamp(value: datetime | None = None) -> str:
    value = value or datetime.now(timezone.utc)
    if value.tzinfo is None:
        raise ValueError("A timezone-aware timestamp is required")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


@dataclass(frozen=True, slots=True)
class UserProfile:
    telegram_user_id: int
    username: str | None = None
    first_name: str | None = None
    last_name: str | None = None


@dataclass(frozen=True, slots=True)
class UserRecord:
    telegram_user_id: int
    username: str | None
    first_name: str | None
    last_name: str | None
    created_at: str
    last_seen_at: str | None
    status: str
    authorized_at: str | None
    is_blocked: bool
    blocked_at: str | None
    block_reason: str | None
    blocked_by: int | None
    block_source: str | None = None


@dataclass(frozen=True, slots=True)
class AccessDecision:
    code: str
    restricted_until: str | None = None
    block_source: str | None = None
    sanction_id: int | None = None

    @property
    def allowed(self) -> bool:
        return self.code == "allowed"


class AccessDeniedError(RuntimeError):
    def __init__(self, decision: AccessDecision) -> None:
        self.decision = decision
        super().__init__(decision.code)


@dataclass(frozen=True, slots=True)
class UserSelection:
    provider: str = "amazon_bedrock"
    model_key: str | None = None


@dataclass(frozen=True, slots=True)
class ModerationEvent:
    telegram_user_id: int
    request_id: str | None
    model: str | None
    provider: str | None
    category: str
    severity: str
    source: str
    action: str
    code: str
    guardrail: str
    timestamp: str
    signal_source: str | None = None
    provider_code: str | None = None
    provider_severity: str | None = None
    confidence: float | None = None
    classifier_model: str | None = None
    classifier_provider: str | None = None
    policy_version: str | None = None
    intent: str | None = None
    confidence_source: str | None = None
    policy_action: str | None = None
    policy_fingerprint: str | None = None
    safety_evaluation_id: int | None = None


@dataclass(frozen=True, slots=True)
class ModerationOutcome:
    event_id: int
    warning_id: int
    active_warning_count: int
    automatic_action: str
    resulting_user_state: str
    sanction_id: int | None = None
    created: bool = True


@dataclass(frozen=True, slots=True)
class AdminCommand:
    admin_id: int
    action: str
    reason: str
    timestamp: str
    target_user_id: int | None = None
    event_id: int | None = None
    expires_at: str | None = None
    daily_request_limit: int | None = None
    review_status: str | None = None
    application_id: int | None = None
    appeal_id: int | None = None
    safety_evaluation_id: int | None = None
    expected_safety_action: str | None = None
    expected_safety_review_id: int | None = None
    policy_fingerprint: str | None = None


@dataclass(frozen=True, slots=True)
class AdminResult:
    audit_id: int
    target_user_id: int
    resource_id: int | None = None


@dataclass(frozen=True, slots=True)
class ActivationResult:
    status: str
    remaining_attempts: int | None = None
    locked_until: str | None = None


@dataclass(frozen=True, slots=True)
class Page:
    items: list[dict]
    page: int
    total: int
    page_size: int = 8

    @property
    def pages(self) -> int:
        return max(1, (self.total + self.page_size - 1) // self.page_size)
