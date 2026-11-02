from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from Storage.interfaces import AdminRepository, ModerationRepository
from Storage.models import AdminCommand, AdminResult, utc_timestamp
from Utils.privacy import private_reason
from state import BotState
from AI.adaptive_safety import policy_fingerprint


@dataclass(slots=True)
class AdminService:
    repository: AdminRepository
    moderation_repository: ModerationRepository
    state: BotState
    admin_ids: frozenset[int]
    secrets: tuple[str, ...] = ()
    safety_fingerprint: str = field(default_factory=policy_fingerprint)

    def is_admin(self, actor: int) -> bool:
        return actor in self.admin_ids

    def require_admin(self, actor: int) -> None:
        if not self.is_admin(actor):
            raise PermissionError("Administrator access required")

    async def act(self, actor: int, action: str, *, user_id: int | None = None,
                  reason: str, event_id: int | None = None, minutes: int | None = None,
                  daily_request_limit: int | None = None,
                  review_status: str | None = None, application_id: int | None = None,
                  appeal_id: int | None = None, safety_evaluation_id: int | None = None,
                  expected_safety_action: str | None = None, expected_safety_review_id: int | None = None) -> AdminResult:
        self.require_admin(actor)
        if user_id is not None and (type(user_id) is not int or user_id <= 0):
            raise ValueError("A positive Telegram user ID is required")
        if event_id is not None and (type(event_id) is not int or event_id <= 0):
            raise ValueError("A positive moderation event ID is required")
        if action in {"approve_trusted", "reject_trusted"}:
            if type(application_id) is not int or application_id <= 0:
                raise ValueError("A pending application is required")
        elif application_id is not None:
            raise ValueError("Application ID is only valid for application decisions")
        if action in {"approve_appeal", "reject_appeal"}:
            if type(appeal_id) is not int or appeal_id <= 0:
                raise ValueError("A pending appeal is required")
        elif appeal_id is not None:
            raise ValueError("Appeal ID is only valid for appeal decisions")
        if action == "review_safety":
            if type(safety_evaluation_id) is not int or safety_evaluation_id <= 0:
                raise ValueError("A safety evaluation is required")
            if expected_safety_action not in {"allow", "safe_complete", "refuse", "violation", "unclear"}:
                raise ValueError("A human expected action is required")
        elif any(v is not None for v in (safety_evaluation_id, expected_safety_action, expected_safety_review_id)):
            raise ValueError("Safety review references are only valid for safety review")
        reason = private_reason(reason, self.secrets)
        if not reason:
            raise ValueError("A reason is required")
        now = datetime.now(timezone.utc)
        expires_at = None
        if action == "restrict":
            if type(minutes) is not int or not 1 <= minutes <= 525600:
                raise ValueError("Restriction duration must be between 1 and 525600 minutes")
            expires_at = utc_timestamp(now + timedelta(minutes=minutes))
        if action == "set_limit" and daily_request_limit is not None:
            if type(daily_request_limit) is not int or not 1 <= daily_request_limit <= 1_000_000:
                raise ValueError("Daily request limit must be positive, or off")
        if action == "review_moderation":
            if event_id is None or review_status not in {"open", "resolved", "dismissed"}:
                raise ValueError("Provide an event ID and open/resolved/dismissed status")
        result = await self.repository.apply(AdminCommand(
            admin_id=actor, action=action, reason=reason, timestamp=utc_timestamp(now),
            target_user_id=user_id, event_id=event_id, expires_at=expires_at,
            daily_request_limit=daily_request_limit, review_status=review_status,
            application_id=application_id, appeal_id=appeal_id,
            safety_evaluation_id=safety_evaluation_id, expected_safety_action=expected_safety_action,
            expected_safety_review_id=expected_safety_review_id, policy_fingerprint=self.safety_fingerprint,
        ))
        if action in {"block", "restrict", "revoke_grok", "revoke_premium"}:
            self.state.reset_user_conversations(result.target_user_id)
        return result

    def clean_reason(self, reason: str) -> str:
        return private_reason(reason, self.secrets)

    async def page(self, actor: int, resource: str, page=0, **filters):
        self.require_admin(actor)
        return await self.repository.page(resource, page, **filters)

    async def detail(self, actor: int, resource: str, resource_id: int) -> dict:
        self.require_admin(actor)
        return await self.repository.detail(resource, resource_id)

    async def dashboard(self, actor: int) -> dict:
        self.require_admin(actor)
        return await self.repository.dashboard(utc_timestamp())

    async def safety_summary(self, actor: int) -> dict:
        self.require_admin(actor)
        return await self.repository.safety_summary(utc_timestamp(), self.safety_fingerprint)

    async def user(self, actor: int, user_id: int) -> dict:
        self.require_admin(actor)
        return await self.repository.user_overview(user_id, utc_timestamp())

    async def events(self, actor: int, user_id: int, limit: int = 20) -> list[dict]:
        self.require_admin(actor)
        return await self.moderation_repository.list_for_user(user_id, limit)

    async def audit(self, actor: int, user_id: int, limit: int = 20) -> list[dict]:
        self.require_admin(actor)
        return await self.repository.audit_for_user(user_id, limit)

    async def runtime_counts(self, actor: int) -> dict[str, int]:
        self.require_admin(actor)
        counts = await self.repository.counts()
        return {**counts, "active_conversations": len(self.state.histories)}
