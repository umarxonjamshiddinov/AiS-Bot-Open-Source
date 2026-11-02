import logging
import re

from aiogram import BaseMiddleware
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, Message

from Handlers.menus import main_menu
from Handlers.flows import ActivationFlow, AppealFlow
from Handlers.moderation import appeal_keyboard
from Handlers.ui import delete_private_input
from Services.access import AccountService
from Services.users import UserService, access_message
from Storage.errors import StorageError
from Storage.models import AccessDeniedError
from state import BotState


class AuthMiddleware(BaseMiddleware):
    """Public Telegram identity entry; persistent bans/restrictions still apply."""

    def __init__(
        self,
        *,
        state: BotState,
        users: UserService,
        accounts: AccountService | None = None,
        support_contact: str = "",
    ) -> None:
        self._state = state
        self._users = users
        self._accounts = accounts
        self._support_contact = support_contact

    async def __call__(self, handler, event, data):
        user = getattr(event, "from_user", None)
        if user is None:
            return None
        try:
            fsm = data.get("state")
            code_flow = fsm is not None and await fsm.get_state() == ActivationFlow.code.state
            text = (getattr(event, "text", None) or getattr(event, "caption", None) or "")
            contains_secret = bool(self._accounts and self._accounts.contains_secret(text))
            expired_flow = data.get("control_flow_expired", False)
            if isinstance(event, Message) and (code_flow or contains_secret or expired_flow):
                await delete_private_input(event)
            await self._users.observe(user)
            decision = await self._users.access(user.id)
            if not decision.allowed:
                self._state.reset_user_conversations(user.id)
                callback_data = getattr(event, "data", "") or ""
                appeal_callback = bool(re.fullmatch(r"appeal:(?:(?:open|status):[1-9][0-9]*|cancel)", callback_data))
                appeal_input = (isinstance(event, Message) and fsm is not None
                                and await fsm.get_state() == AppealFlow.explanation.state
                                and not text.startswith("/"))
                # This exception opens only the appeal handlers. They independently
                # enforce ownership and active automatic-sanction linkage in SQL.
                if appeal_callback or appeal_input:
                    return await handler(event, data)
                if fsm is not None and isinstance(event, Message) and text.startswith("/"):
                    await fsm.clear()
                await self._answer(
                    event,
                    access_message(decision, self._support_contact),
                    appeal_keyboard(decision.sanction_id),
                )
                return None
            if expired_flow:
                await event.answer("Предыдущий диалог уже завершён. Выберите действие в меню.", reply_markup=main_menu())
                return None
            if contains_secret and not code_flow and isinstance(event, Message):
                await event.answer("Для ввода кода откройте «Premium Models» или «Trusted» в меню.", reply_markup=main_menu())
                return None
            return await handler(event, data)
        except AccessDeniedError as error:
            await self._answer(
                event,
                access_message(error.decision, self._support_contact),
                appeal_keyboard(error.decision.sanction_id),
            )
        except StorageError as error:
            logging.getLogger(__name__).error("Storage unavailable type=%s", type(error).__name__)
            await self._answer(event, "❌ Сервис временно недоступен. Попробуйте позже.")
        return None

    @staticmethod
    async def _answer(event, text: str, reply_markup=None) -> None:
        if isinstance(event, Message):
            await event.answer(text, reply_markup=reply_markup)
        elif isinstance(event, CallbackQuery):
            try:
                await event.answer(text, show_alert=True)
                if reply_markup is not None and event.message is not None:
                    await event.message.answer(text, reply_markup=reply_markup)
            except TelegramBadRequest as error:
                message = str(error).lower()
                is_old_query = (
                    "query is too old" in message
                    or "query id is invalid" in message
                    or "response timeout expired" in message
                )
                if not is_old_query:
                    raise
