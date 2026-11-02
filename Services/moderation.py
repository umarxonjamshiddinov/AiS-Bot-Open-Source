from __future__ import annotations

import re
import asyncio
import time
from dataclasses import dataclass

from AI.moderation import ModerationResult, machine_code
from AI.input_moderation import InputModerator, ModerationUnavailableError
from AI.adaptive_safety import AdaptiveModerator
from AI.safety_context import build_safety_context
from AI.safety_policy import POLICY_VERSION, SafetyAction
from AI.telemetry import attempt_observer
from Services.safety_shadow import SafetyProbe, SafetyReviewCache, SafetyShadow, evaluation_record
from Storage.errors import StorageError
from Storage.interfaces import ModerationRepository
from Storage.models import AccessDeniedError, ModerationEvent, ModerationOutcome, utc_timestamp
from Utils.privacy import private_reason


@dataclass(slots=True)
class ModerationService:
    repository: ModerationRepository
    admin_ids: frozenset[int] = frozenset()
    secrets: tuple[str, ...] = ()
    input_moderator: InputModerator | None = None
    adaptive_moderator: AdaptiveModerator | None = None
    shadow: SafetyShadow | None = None
    review_cache: SafetyReviewCache | None = None

    async def check(self, *, prompt, history=(), image_bytes=None, mime_type="image/jpeg", request_id=None,
                    user_id=None, model=None, provider=None, recent_refusals=(), safety_input=None) -> ModerationResult:
        policy = await self.repository.policy_state()
        if policy["policy_version"] == POLICY_VERSION:
            if self.adaptive_moderator is None or policy["policy_fingerprint"] != self.adaptive_moderator.fingerprint:
                raise ModerationUnavailableError("Active policy fingerprint does not match the reviewed engine")
            return await self._adaptive_check(prompt=prompt, history=history, image_bytes=image_bytes, mime_type=mime_type,
                request_id=request_id, user_id=user_id, model=model, provider=provider, recent_refusals=recent_refusals, safety_input=safety_input)
        if self.input_moderator is None:
            raise ModerationUnavailableError("Input moderator is not configured")
        try:
            return await self.input_moderator.check(prompt=prompt, history=history, image_bytes=image_bytes, mime_type=mime_type)
        except (AccessDeniedError, StorageError, asyncio.CancelledError, ModerationUnavailableError):
            raise
        except Exception:
            raise ModerationUnavailableError("Input moderation is temporarily unavailable") from None

    async def _adaptive_check(self, *, prompt, history, image_bytes, mime_type, request_id, user_id,
                              model, provider, recent_refusals, safety_input):
        if not request_id or not user_id:
            raise ModerationUnavailableError("Adaptive enforcement requires a request trace")
        started, result, error = time.perf_counter(), None, None
        probe = SafetyProbe(attempt_observer.get())
        token = attempt_observer.set(probe)
        kind = "image" if image_bytes is not None else (safety_input.kind if safety_input else "text")
        try:
            context = build_safety_context(prompt, history, recent_refusals, safety_input)
            if self.review_cache:
                self.review_cache.put(request_id, user_id, self.adaptive_moderator.fingerprint, context, image_bytes)
            result = await self.adaptive_moderator.assess(context, image_bytes=image_bytes, mime_type=mime_type)
        except BaseException as caught:
            error = caught
        finally:
            attempt_observer.reset(token)
        record = evaluation_record(request_id=request_id, user_id=user_id, model=model, provider=provider,
            classifier=self.adaptive_moderator, context_kind=kind, mode="production", result=result, error=error,
            latency_ms=round((time.perf_counter() - started) * 1000), probe=probe, production_policy=POLICY_VERSION,
            production_action=result.decision.action.value if result else None)
        evaluation_id = await self.repository.save_safety_evaluation(record)
        if error:
            if isinstance(error, (AccessDeniedError, StorageError, asyncio.CancelledError)):
                raise error
            raise ModerationUnavailableError("Adaptive safety check is unavailable") from None
        facts, action = result.assessment, result.decision
        return ModerationResult(
            blocked=action.action in {SafetyAction.REFUSE, SafetyAction.VIOLATION}, source="adaptive_policy_engine",
            code=action.reason_code, category=facts.category if facts else None, severity=facts.severity if facts else None,
            confidence=facts.confidence if facts else None, stage="input", classifier_model=result.model,
            classifier_provider=result.provider, policy_version=POLICY_VERSION, intent=facts.intent if facts else None,
            confidence_source=facts.confidence_source if facts else "unavailable", policy_action=action.action.value,
            policy_fingerprint=result.fingerprint, safety_evaluation_id=evaluation_id, safe_mode=action.safe_mode)

    def start_shadow(self):
        if self.shadow:
            self.shadow.start()

    async def close_shadow(self):
        if self.shadow:
            await self.shadow.close()
        if self.review_cache:
            self.review_cache.clear()

    async def confirm(self, *, user_id: int, request_id: str, model: str | None,
                      provider: str | None, result: ModerationResult) -> ModerationOutcome:
        if not result.blocked or result.effective_action != "violation":
            raise ValueError("Only a confirmed blocked decision can create a warning")
        return await self.repository.confirm(self._event(user_id, request_id, model, provider, result))

    async def refuse(self, *, user_id, request_id, model, provider, result):
        if result.effective_action != "refuse":
            raise ValueError("Nonpunitive refusal requires REFUSE")
        return await self.repository.record_refusal(self._event(user_id, request_id, model, provider, result))

    def _event(self, user_id, request_id, model, provider, result):

        def code(value):
            value = machine_code(value)
            return None if value and any(secret and secret in value for secret in self.secrets) else value

        category, severity = code(result.category), code(result.severity)
        return ModerationEvent(
            telegram_user_id=user_id, request_id=request_id, model=model, provider=provider,
            category=category or "unknown",
            severity=severity if severity in {"unknown", "info", "low", "medium", "high", "critical"} else "unknown",
            source=result.stage, action="block", code=code(result.code) or "provider_moderation",
            guardrail=code(result.policy_version) or "upstream",
            timestamp=utc_timestamp(), signal_source=code(result.source), provider_code=code(result.provider_code),
            provider_severity=severity, confidence=result.confidence,
            classifier_model=code(result.classifier_model), classifier_provider=code(result.classifier_provider),
            policy_version=code(result.policy_version),
            intent=code(result.intent), confidence_source=code(result.confidence_source), policy_action=result.effective_action,
            policy_fingerprint=code(result.policy_fingerprint), safety_evaluation_id=result.safety_evaluation_id,
        )

    async def block_for_user(self, user_id: int, sanction_id: int | None = None) -> dict:
        return await self.repository.block_for_user(user_id, sanction_id)

    async def appeal(self, user_id: int, sanction_id: int, explanation: str) -> dict:
        if type(user_id) is not int or user_id <= 0 or type(sanction_id) is not int or sanction_id <= 0:
            raise ValueError("Invalid appeal reference")
        if not isinstance(explanation, str) or not 1 <= len(explanation.strip()) <= 1000:
            raise ValueError("An explanation of 1 to 1000 characters is required")
        explanation = private_reason(explanation, self.secrets, max_length=1000)
        if not explanation:
            raise ValueError("An explanation is required")
        return await self.repository.create_appeal(
            user_id=user_id, sanction_id=sanction_id, explanation=explanation,
            timestamp=utc_timestamp(), admin_ids=tuple(sorted(self.admin_ids)))

    async def appeal_for_user(self, user_id: int, appeal_id: int) -> dict:
        return await self.repository.appeal_for_user(user_id, appeal_id)

    async def record(self, *, user_id: int, request_id: str | None, model: str | None,
                     provider: str | None, category: str, severity: str, source: str,
                     action: str, code: str, guardrail: str) -> int:
        # Trusted classifier metadata only. There is deliberately no prompt,
        # completion, excerpt, exception body, or arbitrary metadata parameter.
        for value in (category, code, guardrail):
            if not re.fullmatch(r"[a-zA-Z0-9_.:-]{1,80}", value):
                raise ValueError("Moderation metadata must be a bounded machine code")
        return await self.repository.record(ModerationEvent(
            user_id, request_id, model, provider, category, severity, source,
            action, code, guardrail, utc_timestamp(),
        ))
