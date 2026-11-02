from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

from AI.models import Modality, ModelInfo, get_model
from AI.prompts import system_prompt_for
from AI.providers import (
    AMAZON_BEDROCK,
    backend_display_name,
    model_for_backend,
    models_for_backend,
)
from Search.web_search import WebSearchService, is_model_query, should_search
from state import BotState, ConversationId, ConversationKey, ConversationSnapshot
from Utils.logging import safe_conversation_id

logger = logging.getLogger(__name__)


class UnsupportedModalityError(RuntimeError):
    pass


class StaleConversationError(RuntimeError):
    """A result belonging to a reset conversation must be silently discarded."""


@dataclass(frozen=True, slots=True)
class AIResponse:
    text: str
    model_key: str
    route: str
    web_search_used: bool
    inference_count: int
    helper_used: bool
    helper_model_key: str | None
    latency_ms: int


class AIRouter:
    def __init__(
        self,
        *,
        client,
        search: WebSearchService,
        state: BotState,
        max_concurrent_requests: int,
        default_max_output_tokens: int,
        semaphore: asyncio.Semaphore | None = None,
    ) -> None:
        if max_concurrent_requests < 2:
            raise ValueError("Global concurrency limit must be at least 2")
        self._client = client
        self._search = search
        self._state = state
        self._semaphore = semaphore or asyncio.Semaphore(max_concurrent_requests)
        self._default_max_output_tokens = default_max_output_tokens

    async def ask_text(
        self,
        *,
        conversation_key: ConversationKey,
        conversation_id: ConversationId,
        model_key: str,
        prompt: str,
        provider: str = AMAZON_BEDROCK,
        web_query: str | None = None,
    ) -> AIResponse:
        return await self._ask(
            conversation_key=conversation_key,
            conversation_id=conversation_id,
            model_key=model_key,
            provider=provider,
            prompt=prompt,
            modality=Modality.TEXT,
            web_query=web_query,
        )

    async def ask_image(
        self,
        *,
        conversation_key: ConversationKey,
        conversation_id: ConversationId,
        model_key: str,
        prompt: str,
        provider: str = AMAZON_BEDROCK,
        image_bytes: bytes,
        mime_type: str,
    ) -> AIResponse:
        return await self._ask(
            conversation_key=conversation_key,
            conversation_id=conversation_id,
            model_key=model_key,
            provider=provider,
            prompt=prompt,
            modality=Modality.IMAGE,
            image_bytes=image_bytes,
            mime_type=mime_type,
            web_query=prompt,
        )

    async def _ask(
        self,
        *,
        conversation_key: ConversationKey,
        conversation_id: ConversationId,
        model_key: str,
        prompt: str,
        provider: str,
        modality: Modality,
        image_bytes: bytes | None = None,
        mime_type: str = "image/jpeg",
        web_query: str | None = None,
    ) -> AIResponse:
        if modality not in {Modality.TEXT, Modality.IMAGE}:
            raise UnsupportedModalityError(
                f"The {modality.value} input is registered but not yet available"
            )
        selected = model_for_backend(provider, model_key)
        if not selected.user_visible:
            raise ValueError("Internal safeguard models cannot be used for chat")

        snapshot = ConversationSnapshot(conversation_key, conversation_id)
        self._ensure_current(snapshot)
        request_lock = self._state.lock_for(conversation_key)
        async with request_lock:
            # Required for old messages that were queued on the previous lock.
            self._ensure_current(snapshot)
            started = time.perf_counter()
            async with self._semaphore:
                # A reset may occur while this request waits for global capacity.
                self._ensure_current(snapshot)
                response = await self._route_locked(
                    snapshot=snapshot,
                    selected=selected,
                    prompt=prompt,
                    modality=modality,
                    image_bytes=image_bytes,
                    mime_type=mime_type,
                    web_query=web_query,
                )
            latency_ms = round((time.perf_counter() - started) * 1000)
            final = AIResponse(
                text=response.text,
                model_key=response.model_key,
                route=response.route,
                web_search_used=response.web_search_used,
                inference_count=response.inference_count,
                helper_used=response.helper_used,
                helper_model_key=response.helper_model_key,
                latency_ms=latency_ms,
            )
            logger.info(
                "AI request conversation=%s generation=%s model=%s model_id=%s "
                "modality=%s route=%s web=%s inferences=%s helper=%s "
                "helper_model=%s latency_ms=%s",
                safe_conversation_id(*conversation_key),
                conversation_id[:12],
                selected.key,
                selected.model_id,
                modality.value,
                final.route,
                final.web_search_used,
                final.inference_count,
                final.helper_used,
                final.helper_model_key or "-",
                final.latency_ms,
            )
            return final

    async def _route_locked(
        self,
        *,
        snapshot: ConversationSnapshot,
        selected: ModelInfo,
        prompt: str,
        modality: Modality,
        image_bytes: bytes | None,
        mime_type: str,
        web_query: str | None,
    ) -> AIResponse:
        query = (web_query or prompt).strip()
        use_web = should_search(query)
        results: list[dict[str, str]] = []
        web_context = ""
        if use_web:
            try:
                results = await self._search.search(query)
            except Exception as error:
                logger.warning("Web search failed type=%s", type(error).__name__)
                results = []
            web_context = self._search.context(results)
            self._ensure_current(snapshot)

        fitted_prompt = self._fit_prompt(prompt, selected)
        if is_model_query(prompt):
            fitted_prompt = self._fit_prompt(
                f"{fitted_prompt}\n\n{self._configured_model_context()}", selected
            )
        if web_context:
            fitted_prompt = self._fit_prompt(
                f"{fitted_prompt}\n\n{web_context}", selected
            )

        history = self._trim_history(
            self._state.history_for(snapshot.key), selected
        )
        inference_count = 0

        if modality is Modality.IMAGE:
            if image_bytes is None:
                raise ValueError("Image data is missing")
            self._ensure_current(snapshot)
            answer = await self._complete(
                selected, [*history, {"role": "user", "content": fitted_prompt}],
                image_bytes=image_bytes, mime_type=mime_type,
            )
            self._ensure_current(snapshot)
            route = "ais_vision"
            inference_count = 1  # Gateway calls; upstream attempts belong to AiS API.
            stored_prompt = f"{prompt}\n[The user attached an image.]"
        else:
            self._ensure_current(snapshot)
            answer = await self._complete(
                selected, [*history, {"role": "user", "content": fitted_prompt}]
            )
            self._ensure_current(snapshot)
            route = "text"
            inference_count = 1
            stored_prompt = prompt

        if use_web:
            answer = self._search.append_real_sources(answer, results)

        # No await occurs between this generation check and the state mutation.
        self._ensure_current(snapshot)
        persisted = self._state.history_for(snapshot.key)
        persisted.extend(
            [
                {"role": "user", "content": self._fit_prompt(stored_prompt, selected)},
                {"role": "assistant", "content": answer},
            ]
        )
        persisted[:] = self._trim_history(persisted, selected)

        return AIResponse(
            text=answer,
            model_key=selected.key,
            route=route,
            web_search_used=use_web,
            inference_count=inference_count,
            helper_used=False,
            helper_model_key=None,
            latency_ms=0,
        )

    def visual_route(self, model_key: str) -> str:
        model = get_model(model_key)
        if model.user_visible:
            return "ais_vision"
        return "internal_only"

    def _ensure_current(self, snapshot: ConversationSnapshot) -> None:
        if not self._state.is_current(snapshot):
            logger.info(
                "Discarding stale request conversation=%s generation=%s",
                safe_conversation_id(*snapshot.key),
                snapshot.conversation_id[:12],
            )
            raise StaleConversationError(
                f"Conversation generation {snapshot.conversation_id} is stale"
            )

    async def _complete(
        self,
        model: ModelInfo,
        messages: list[dict],
        *,
        image_bytes: bytes | None = None,
        mime_type: str = "image/jpeg",
        max_tokens: int | None = None,
    ) -> str:
        output_limit = max_tokens or self._output_limit(model)
        return await self._client.complete(
            model=model,
            messages=messages,
            system_prompt=system_prompt_for(
                model, backend_display_name(model.backend_provider)
            ),
            max_tokens=output_limit,
            image_bytes=image_bytes,
            mime_type=mime_type,
        )

    def _trim_history(
        self, history: list[dict[str, str]], model: ModelInfo
    ) -> list[dict[str, str]]:
        output_reserve = self._output_limit(model)
        input_budget = max(1_000, int(model.context_window * 0.75) - output_reserve)
        char_budget = input_budget * 4
        kept: list[dict[str, str]] = []
        used = 0
        for message in reversed(history[-80:]):
            content = str(message.get("content", ""))
            size = len(content)
            if kept and used + size > char_budget:
                break
            if not kept and size > char_budget:
                content = content[-char_budget:]
                kept.append({**message, "content": content})
                break
            kept.append(message)
            used += size
        kept.reverse()
        return kept

    def _fit_prompt(self, prompt: str, model: ModelInfo) -> str:
        output_reserve = self._output_limit(model)
        max_chars = max(4_000, (model.context_window - output_reserve - 1_000) * 4)
        if len(prompt) <= max_chars:
            return prompt
        marker = "\n\n[Content shortened to fit the selected model's context window.]\n\n"
        head = int(max_chars * 0.75)
        tail = max_chars - head - len(marker)
        return prompt[:head] + marker + prompt[-max(0, tail):]

    def _output_limit(self, model: ModelInfo) -> int:
        context_safe_limit = max(256, model.context_window // 4)
        return min(
            self._default_max_output_tokens,
            model.max_output or self._default_max_output_tokens,
            context_safe_limit,
        )

    def _configured_model_context(self) -> str:
        models = models_for_backend(AMAZON_BEDROCK)
        rows = [
            f"- {model.display_name} | provider={model.backend_provider} | "
            f"model_id={model.model_id}"
            for model in models
        ]
        return (
            "TRUSTED AIS MODEL CATALOG (configured deployments; do not deny these "
            "names as nonexistent):\n" + "\n".join(rows)
        )
