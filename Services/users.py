from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import random

from AI.access import requires_premium, model_is_selectable
from AI.providers import model_for_backend
from Storage.interfaces import UserRepository
from Storage.models import (
    AccessDecision, AccessDeniedError, UserProfile, UserRecord, UserSelection, utc_timestamp,
)
from state import BotState


def access_message(decision: AccessDecision, support_contact: str = "") -> str:
    support = (
        f" Поддержка: {support_contact}."
        if support_contact
        else " Обратитесь к администратору бота."
    )
    if decision.code == "blocked":
        return "⛔ Доступ заблокирован." + support
    if decision.code == "restricted":
        until = datetime.fromisoformat(decision.restricted_until).strftime("%Y-%m-%d %H:%M UTC")
        return f"⏳ Доступ временно ограничен до {until}."
    if decision.code == "limit_exceeded":
        return "⏳ Дневной лимит AI-вызовов исчерпан. Он обновится в 00:00 UTC."
    if decision.code in {"premium_locked", "grok_locked"}:
        return "🔒 Для этой модели нужен Premium Models access. Откройте раздел «Premium Models» в меню."
    if decision.code == "higher_cost_limit_exceeded":
        return "⏳ Общий дневной лимит Higher-cost моделей исчерпан. Выберите Standard-модель или дождитесь 00:00 UTC."
    return "⛔ Доступ недоступен." + support


@dataclass(slots=True)
class UserService:
    repository: UserRepository
    state: BotState | None = None

    async def observe(self, user) -> UserRecord:
        return await self.repository.observe(UserProfile(
            telegram_user_id=user.id, username=getattr(user, "username", None),
            first_name=getattr(user, "first_name", None), last_name=getattr(user, "last_name", None),
        ), utc_timestamp())

    async def access(self, user_id: int) -> AccessDecision:
        return await self.repository.access(user_id, utc_timestamp())

    async def require_access(self, user_id: int) -> None:
        decision = await self.access(user_id)
        if not decision.allowed:
            raise AccessDeniedError(decision)

    async def require_model_access(self, user_id: int, provider: str, model_key: str) -> None:
        model = model_for_backend(provider, model_key)
        if not model_is_selectable(model):
            raise ValueError("Internal or unavailable models cannot be used for chat")
        decision = await self.repository.access(user_id, utc_timestamp(), requires_premium=requires_premium(model))
        if not decision.allowed:
            raise AccessDeniedError(decision)

    async def authorize(self, user_id: int) -> AccessDecision:
        return await self.repository.authorize(user_id, utc_timestamp())

    async def selection(self, key: tuple[int, int]) -> UserSelection:
        selection = await self.repository.selection(*key)
        if selection.model_key is not None:
            try:
                model = model_for_backend(selection.provider, selection.model_key)
                if not model.user_visible:
                    return UserSelection()
            except KeyError:
                # A removed provider/model cannot make a restored setting unsafe.
                return UserSelection()
        return selection

    async def select_model(self, key: tuple[int, int], provider: str, model_key: str) -> None:
        model = model_for_backend(provider, model_key)
        if not model_is_selectable(model):
            raise ValueError("Internal safeguard models cannot be selected")
        changed = await self.repository.select_model(
            *key, UserSelection(provider, model_key), utc_timestamp(), requires_premium=requires_premium(model))
        if changed and self.state is not None:
            self.state.reset_conversation(*key)

    async def random_model(self, key: tuple[int, int], provider: str = "amazon_bedrock"):
        candidates = await self.repository.available_models(key[1], utc_timestamp(), provider)
        if not candidates:
            raise ValueError("No models are currently available")
        model = random.choice(candidates)
        # Selection persists normally and rechecks access/quota after the roll.
        await self.select_model(key, provider, model.key)
        return model
