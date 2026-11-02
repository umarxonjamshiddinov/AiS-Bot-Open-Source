import unittest
from unittest.mock import patch

from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import AnswerCallbackQuery
from aiogram.types import CallbackQuery, User

from Handlers.auth import AuthMiddleware
from state import BotState
from Tests.storage_support import StorageTestCase


def _callback(user_id: int = 1) -> CallbackQuery:
    return CallbackQuery(
        id="cb-1",
        from_user=User(id=user_id, is_bot=False, first_name="Test"),
        chat_instance="chat-instance",
    )


def _bad_request(message: str) -> TelegramBadRequest:
    method = AnswerCallbackQuery(callback_query_id="cb-1")
    return TelegramBadRequest(method=method, message=message)


class AuthMiddlewareCallbackTests(StorageTestCase):
    async def test_ignores_stale_callback_errors(self) -> None:
        await self.admin.act(999, "block", user_id=1, reason="Callback access test")
        middleware = AuthMiddleware(state=BotState(), users=self.users)
        stale_messages = (
            "query is too old",
            "query ID is invalid",
            "response timeout expired",
            "query is too old and response timeout expired or query ID is invalid",
        )

        for message in stale_messages:
            with self.subTest(message=message):
                callback = _callback()
                handler_called = False

                async def handler(event, data):
                    nonlocal handler_called
                    handler_called = True
                    return "handled"

                with patch.object(
                    CallbackQuery,
                    "answer",
                    side_effect=_bad_request(message),
                ):
                    result = await middleware(handler, callback, {})

                self.assertIsNone(result)
                self.assertFalse(handler_called)

    async def test_reraises_non_stale_telegram_bad_request(self) -> None:
        await self.admin.act(999, "block", user_id=1, reason="Callback access test")
        middleware = AuthMiddleware(state=BotState(), users=self.users)
        callback = _callback()

        async def handler(event, data):
            return "handled"

        error = _bad_request("chat not found")
        with patch.object(CallbackQuery, "answer", side_effect=error):
            with self.assertRaises(TelegramBadRequest):
                await middleware(handler, callback, {})
