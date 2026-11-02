from __future__ import annotations

import asyncio
import time
import logging
from dataclasses import dataclass, field
from dataclasses import replace
from uuid import uuid4

from AI.access import requires_premium
from AI.providers import model_for_backend
from AI.router import AIRouter, AIResponse, StaleConversationError
from AI.safety_policy import POLICY_VERSION, safe_completion_mode
from AI.safety_context import SafetyInput
from AI.safety_client import selected_safety_model, safety_receipt
from Services.safety_shadow import ShadowJob
from AI.telemetry import (
    ProviderModerationError, TokenUsage, attempt_observer, attempt_purpose, safe_error_code, main_inference,
)
from Services.moderation import ModerationService
from Services.users import UserService
from Storage.interfaces import UsageRepository
from Storage.models import AccessDeniedError, utc_timestamp
from state import BotState, ConversationSnapshot


@dataclass(slots=True)
class _RequestObserver:
    repository: UsageRepository
    request_id: str
    user_id: int

    async def start(self, model) -> str:
        attempt_id = uuid4().hex
        purpose = attempt_purpose.get()
        decision = await self.repository.start_attempt(
            attempt_id=attempt_id, request_id=self.request_id, user_id=self.user_id,
            timestamp=utc_timestamp(), model=model.key, provider=model.backend_provider,
            requires_premium=purpose == "product" and main_inference.get() and requires_premium(model), purpose=purpose,
            is_main_inference=main_inference.get(),
        )
        if not decision.allowed:
            raise AccessDeniedError(decision)
        return attempt_id

    async def finish(self, *, attempt_id: str, model, status: str, latency_ms: int,
                     usage: TokenUsage, error: BaseException | None, inference_started: bool = True) -> None:
        await self.repository.finish_attempt(
            attempt_id=attempt_id, status=status, latency_ms=latency_ms,
            input_tokens=usage.input_tokens, output_tokens=usage.output_tokens,
            total_tokens=usage.total_tokens, error_type=type(error).__name__ if error else None,
            error_code=safe_error_code(error) if error else None,
            counts_toward_limit=inference_started and attempt_purpose.get() == "product" and main_inference.get(),
        )


@dataclass(slots=True)
class AIRequestService:
    router: AIRouter
    users: UserService
    usage: UsageRepository
    moderation: ModerationService
    state: BotState
    _active: set[asyncio.Task] = field(default_factory=set, init=False)
    _closing: bool = field(default=False, init=False)

    async def ask_text(self, **kwargs) -> AIResponse:
        return await self._run(self.router.ask_text, kwargs)

    async def ask_image(self, **kwargs) -> AIResponse:
        return await self._run(self.router.ask_image, kwargs)

    async def _run(self, operation, kwargs) -> AIResponse:
        if self._closing:
            raise RuntimeError("Application is shutting down")
        task = asyncio.current_task()
        self._active.add(task)
        try:
            return await self._request(operation, kwargs)
        finally:
            self._active.discard(task)

    async def _request(self, operation, kwargs) -> AIResponse:
        key = kwargs["conversation_key"]
        user_id = key[1]
        request_id = uuid4().hex
        model = kwargs["model_key"]
        provider = kwargs.get("provider", "amazon_bedrock")
        selected = model_for_backend(provider, model)
        started = time.perf_counter()
        decision = await self.usage.start_request(
            request_id=request_id, user_id=user_id, timestamp=utc_timestamp(),
            model=model, provider=provider, requires_premium=requires_premium(selected),
        )
        if not decision.allowed:
            raise AccessDeniedError(decision)
        observer = _RequestObserver(self.usage, request_id, user_id)
        token = attempt_observer.set(observer)
        safety_token = selected_safety_model.set(model)
        receipt_token = safety_receipt.set(None)
        status = "error"
        error_type = None
        error_code = None
        moderation_blocked = False
        moderation, event_id, production_action = None, None, None
        safety_input = kwargs.pop("safety_input", None)
        history = list(self.state.histories.get(key, ()))
        recent_refusals = list(self.state.safety_refusals.get(key, ()))
        moderation_latency_ms = None
        instruction_token = safe_completion_mode.set(None)
        try:
            if not selected.user_visible:
                raise ValueError("Internal safeguard models cannot be used for chat")
            image_bytes = kwargs.get("image_bytes")
            if operation == self.router.ask_image and (not isinstance(image_bytes, bytes) or not image_bytes):
                raise ValueError("An image request requires image bytes")
            if image_bytes is not None and safety_input is None:
                safety_input = SafetyInput(kwargs["prompt"], kind="image")
            snapshot = ConversationSnapshot(key, kwargs["conversation_id"])
            if not self.state.is_current(snapshot):
                raise StaleConversationError("Conversation changed before input moderation")
            try:
                moderation_started = time.perf_counter()
                moderation = await self.moderation.check(
                    prompt=kwargs["prompt"], history=history, image_bytes=image_bytes, mime_type=kwargs.get("mime_type", "image/jpeg"),
                    request_id=request_id, user_id=user_id, model=model, provider=provider,
                    recent_refusals=recent_refusals, safety_input=safety_input)
                moderation_latency_ms = round((time.perf_counter() - moderation_started) * 1000)
            except BaseException:
                await self.usage.record_input_moderation(request_id=request_id, status="error", timestamp=utc_timestamp())
                raise
            await self.usage.record_input_moderation(
                request_id=request_id, status="blocked" if moderation.blocked else "allowed", timestamp=utc_timestamp(),
                source=moderation.source, model=moderation.classifier_model,
                provider=moderation.classifier_provider, policy=moderation.policy_version, safety_action=moderation.effective_action)
            production_action = moderation.effective_action
            if moderation.blocked:
                raise ProviderModerationError(result=moderation, model=model, provider=provider)
            if moderation.effective_action == "safe_complete":
                safe_completion_mode.set(moderation.safe_mode)
            if not self.state.is_current(snapshot):
                raise StaleConversationError("Conversation changed during input moderation")
            await self.users.require_model_access(user_id, provider, model)
            result = await operation(**kwargs)
            await self.users.require_model_access(user_id, provider, model)
            status = "succeeded"
            return result
        except AccessDeniedError as error:
            status = error.decision.code if error.decision.code in {"blocked", "restricted", "unauthorized", "limit_exceeded"} else "blocked"
            error_type, error_code = type(error).__name__, error.decision.code
            if error.decision.code not in {"limit_exceeded", "higher_cost_limit_exceeded"}:
                self.state.reset_conversation(*key)
            raise
        except StaleConversationError as error:
            status, error_type = "stale", type(error).__name__
            raise
        except ProviderModerationError as error:
            status, error_type, error_code = "blocked", type(error).__name__, error.code
            moderation_blocked = True
            if moderation is not None and moderation.policy_version == POLICY_VERSION and error.result.policy_version != POLICY_VERSION:
                # v5 never infers intent/confidence from a provider refusal alone.
                error.result = replace(error.result, policy_version=POLICY_VERSION, policy_action="refuse",
                                       policy_fingerprint=moderation.policy_fingerprint,
                                       confidence_source="provider" if error.result.confidence is not None else "unavailable")
            production_action = error.result.effective_action
            if production_action == "refuse":
                event_id = await self.moderation.refuse(user_id=user_id, request_id=request_id,
                    model=error.model or model, provider=error.provider or provider, result=error.result)
            else:
                error.outcome = await self.moderation.confirm(
                    user_id=user_id, request_id=request_id, model=error.model or model,
                    provider=error.provider or provider, result=error.result)
                event_id = error.outcome.event_id
                if error.outcome.resulting_user_state == "blocked":
                    self.state.reset_user_conversations(user_id)
            self.state.remember_safety_refusal(snapshot, kwargs["prompt"])
            raise
        except asyncio.CancelledError as error:
            status, error_type = "cancelled", type(error).__name__
            raise
        except Exception as error:
            error_type, error_code = type(error).__name__, safe_error_code(error)
            raise
        finally:
            attempt_observer.reset(token)
            selected_safety_model.reset(safety_token)
            safety_receipt.reset(receipt_token)
            safe_completion_mode.reset(instruction_token)
            await self.usage.finish_request(
                request_id=request_id, status=status, timestamp=utc_timestamp(),
                latency_ms=round((time.perf_counter() - started) * 1000),
                error_type=error_type, error_code=error_code, moderation_blocked=moderation_blocked,
            )
            if moderation is not None and moderation.policy_version != POLICY_VERSION and self.moderation.shadow:
                try:
                    self.moderation.shadow.schedule(ShadowJob(
                        request_id, user_id, model, provider, kwargs["prompt"], history, recent_refusals, safety_input,
                        kwargs.get("image_bytes"), kwargs.get("mime_type", "image/jpeg"), production_action,
                        moderation.policy_version or "ais_safety_v1", status, moderation_latency_ms, event_id))
                except Exception as error:
                    # Shadow failure must never replace v4's result or exception.
                    logging.getLogger(__name__).error("Shadow scheduling failed type=%s", type(error).__name__)

    async def close(self) -> None:
        self._closing = True
        tasks = [task for task in self._active if task is not asyncio.current_task()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
