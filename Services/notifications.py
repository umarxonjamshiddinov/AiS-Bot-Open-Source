from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from html import escape

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from Storage.interfaces import NotificationRepository
from Storage.models import utc_timestamp


@dataclass(slots=True)
class NotificationService:
    repository: NotificationRepository
    bot: object
    _signal: asyncio.Event = field(default_factory=asyncio.Event, init=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)
    _task: asyncio.Task | None = field(default=None, init=False)

    def start(self):
        self._task = asyncio.create_task(self._run(), name="account-moderation-notifications")

    def wake(self):
        self._signal.set()

    async def _run(self):
        while True:
            self._signal.clear()
            try:
                await self.flush()
            except Exception as error:
                logging.getLogger(__name__).error("Notification queue failed type=%s", type(error).__name__)
            try:
                await asyncio.wait_for(self._signal.wait(), timeout=5)
            except TimeoutError:
                pass

    async def flush(self):
        async with self._lock:
            for item in await self.repository.pending(utc_timestamp()):
                now = datetime.now(timezone.utc)
                if item["kind"] in {"trusted_pending", "appeal_pending"} and item["application_status"] != "pending":
                    await self.repository.finish(item["id"], timestamp=utc_timestamp(now), status="sent",
                                                  next_attempt_at=utc_timestamp(now), error_code="superseded")
                    continue
                if item["kind"] == "appeal_pending":
                    name = "@" + item["username"] if item["username"] else item["first_name"] or "Пользователь"
                    text = f"<b>📨 Новая апелляция #{item['appeal_id']}</b>\n{escape(name)} · <code>{item['telegram_user_id']}</code>"
                    button = InlineKeyboardButton(text="Открыть апелляцию", callback_data=f"adm:appeal:{item['appeal_id']}")
                elif item["kind"] in {"appeal_approved", "appeal_rejected"}:
                    if item["kind"] == "appeal_approved":
                        text = ("✅ Ваша апелляция удовлетворена. Связанное предупреждение отменено."
                                "\nАвтоматическая блокировка по этому решению снята.")
                        if item["resulting_user_state"] != "allowed":
                            text += "\nДоступ остаётся ограничен другим решением."
                    else:
                        text = "❌ Ваша апелляция отклонена. Автоматическая блокировка остаётся."
                    text += f"\nПредупреждений на момент решения: {item['resulting_warning_count']}/3."
                    button = InlineKeyboardButton(text="📨 Статус апелляции", callback_data=f"appeal:status:{item['appeal_id']}")
                elif item["kind"] == "trusted_pending":
                    name = "@" + item["username"] if item["username"] else item["first_name"] or "Пользователь"
                    text = f"<b>📨 Новая заявка Trusted</b>\n{escape(name)} · <code>{item['telegram_user_id']}</code>"
                    button = InlineKeyboardButton(text="Открыть заявку", callback_data=f"adm:app:{item['application_id']}")
                else:
                    text = "⭐ Ваша заявка Trusted одобрена." if item["kind"] == "trusted_approved" else "Ваша заявка Trusted отклонена."
                    button = InlineKeyboardButton(text="ℹ️ Мой статус", callback_data="acct:status")
                status, error_code = "sent", None
                try:
                    await self.bot.send_message(item["recipient_id"], text, parse_mode="HTML",
                                                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[button]]))
                except Exception as error:
                    error_code = type(error).__name__
                    permanent = isinstance(error, (TelegramForbiddenError, TelegramBadRequest))
                    status = "failed" if permanent or item["attempts"] >= 9 else "pending"
                delay = min(3600, 30 * (2 ** min(item["attempts"], 7)))
                await self.repository.finish(item["id"], timestamp=utc_timestamp(now), status=status,
                                              next_attempt_at=utc_timestamp(now + timedelta(seconds=delay)), error_code=error_code)

    async def close(self):
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
