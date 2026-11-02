from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import EditMessageText
from aiogram.types import Chat, Message, User

from AI.providers import AMAZON_BEDROCK, available_providers
from Handlers.auth import AuthMiddleware
from Handlers.chat import text_handler
from Handlers.menus import all_callback_data, main_menu, providers_menu
from Handlers.models import select_model, support_handler
from Handlers.start import start_handler
from state import BotState
from Tests.storage_support import StorageTestCase


def telegram_message(text: str) -> Message:
    return Message(
        message_id=1,
        date=datetime.now(timezone.utc),
        chat=Chat(id=1, type="private"),
        from_user=User(id=2, is_bot=False, first_name="Tester"),
        text=text,
    )


class UserUiTests(StorageTestCase):
    async def select(self, model_key):
        await self.add_user(2)
        if model_key.startswith("grok_"):
            await self.admin.act(999, "grant_grok", user_id=2, reason="UI fixture")
        await self.users.select_model((1, 2), AMAZON_BEDROCK, model_key)

    async def test_entry_is_public_and_creates_standard_access(self) -> None:
        state = BotState()
        middleware = AuthMiddleware(state=state, users=self.users)
        handler = AsyncMock()
        answer = AsyncMock()

        with patch.object(Message, "answer", answer):
            await middleware(handler, telegram_message("/start"), {})
            handler.assert_awaited_once()
            answer.assert_not_awaited()
            self.assertTrue((await self.users.access(2)).allowed)
            self.assertEqual((await self.accounts.status(2))["tier"], "STANDARD")

    async def test_start_and_menus_offer_account_access(self) -> None:
        state = BotState()
        message = telegram_message("/start")
        answer = AsyncMock()
        with patch.object(Message, "answer", answer):
            await start_handler(message, SimpleNamespace(state=state))
        self.assertEqual(
            answer.call_args.args[0],
            "👋 Привет! Я AiS.\n\nВыберите модель и начните разговор. Ваш доступ и настройки сохраняются автоматически.",
        )
        self.assertEqual(available_providers(), ((AMAZON_BEDROCK, "☁️ AiS API"),))
        self.assertEqual(
            [button.text for row in main_menu().inline_keyboard for button in row],
            ["🤖 Модели", "💬 Новый чат", "⭐ Trusted", "🔓 Premium Models", "👤 Мой статус", "✉️ Поддержка"],
        )
        self.assertNotIn("image_generation", all_callback_data())
        self.assertIn("support", all_callback_data())
        self.assertIn(
            "☁️ AiS API",
            [button.text for row in providers_menu().inline_keyboard for button in row],
        )

    async def test_model_selection_uses_display_name_and_bedrock(self) -> None:
        state = BotState()
        await self.add_user(2)
        await self.admin.act(999, "grant_grok", user_id=2, reason="UI fixture")
        edited = AsyncMock()
        callback = SimpleNamespace(
            data="model:amazon_bedrock:xAI:grok_4_6",
            message=SimpleNamespace(chat=SimpleNamespace(id=1), edit_text=edited),
            from_user=SimpleNamespace(id=2),
            answer=AsyncMock(),
        )
        await select_model(callback, SimpleNamespace(state=state, users=self.users))
        self.assertEqual((await self.users.selection((1, 2))).model_key, "grok_4_6")
        self.assertEqual((await self.users.selection((1, 2))).provider, AMAZON_BEDROCK)
        self.assertEqual(
            edited.call_args.args[0],
            "✅ Модель: Grok 4.6\n\nНапишите сообщение. При смене модели контекст начинается заново.",
        )

    async def test_identity_uses_the_selected_grok_model_and_provider(self) -> None:
        state = BotState()
        await self.select("grok_4_6")
        answer = AsyncMock()
        bot = SimpleNamespace(send_chat_action=AsyncMock())
        ai_router = SimpleNamespace(ask_text=AsyncMock())
        with patch.object(Message, "answer", answer):
            await text_handler(
                telegram_message("Are you Grok?"),
                bot,
                SimpleNamespace(state=state, users=self.users, requests=ai_router),
            )
        self.assertEqual(
            answer.call_args.args[0],
            "I'm AiS. Under the hood, I'm currently powered by Grok 4.6 "
            "through AiS API.",
        )
        ai_router.ask_text.assert_not_awaited()

    async def test_support_menu_uses_self_host_contact_or_owner_fallback(self) -> None:
        edited = AsyncMock()
        callback = SimpleNamespace(
            message=SimpleNamespace(edit_text=edited),
            answer=AsyncMock(),
        )
        await support_handler(callback, SimpleNamespace(settings=SimpleNamespace(support_contact="help@example.test")))
        self.assertIn("help@example.test", edited.call_args.args[0])
        self.assertEqual(edited.call_args.kwargs["parse_mode"], "HTML")

        await support_handler(callback, SimpleNamespace(settings=SimpleNamespace(support_contact="")))
        self.assertIn("владельцем бота", edited.call_args.args[0])

    async def test_repeated_support_click_is_acknowledged(self) -> None:
        edited = AsyncMock(side_effect=TelegramBadRequest(
            method=EditMessageText(chat_id=1, message_id=2, text="Support"),
            message="Bad Request: message is not modified",
        ))
        callback = SimpleNamespace(
            message=SimpleNamespace(edit_text=edited),
            answer=AsyncMock(),
        )
        await support_handler(callback, SimpleNamespace(settings=SimpleNamespace(support_contact="")))
        callback.answer.assert_awaited_once()

    async def test_identity_changes_with_selected_model(self) -> None:
        state = BotState()
        await self.select("gpt_oss_120b")
        answer = AsyncMock()
        with patch.object(Message, "answer", answer):
            await text_handler(
                telegram_message("Какая ты модель?"),
                SimpleNamespace(send_chat_action=AsyncMock()),
                SimpleNamespace(state=state, users=self.users, requests=SimpleNamespace(ask_text=AsyncMock())),
            )
        self.assertEqual(
            answer.call_args.args[0],
            "I'm AiS. Under the hood, I'm currently powered by GPT OSS 120B "
            "through AiS API.",
        )

    async def test_model_error_does_not_expose_exception_details(self) -> None:
        state = BotState()
        await self.select("gpt_oss_120b")
        failing_router = SimpleNamespace(ask_text=AsyncMock(side_effect=RuntimeError("failure")))
        answer = AsyncMock()
        bot = SimpleNamespace(send_chat_action=AsyncMock())
        with patch.object(Message, "answer", answer):
            await text_handler(
                telegram_message("Hello"),
                bot,
                SimpleNamespace(state=state, users=self.users, requests=failing_router),
            )
        self.assertEqual(
            answer.call_args.args[0],
            "❌ Не удалось получить ответ модели. Попробуйте позже.",
        )

    async def test_normal_text_message_reaches_selected_model(self) -> None:
        state = BotState()
        await self.select("gpt_oss_120b")
        ai_router = SimpleNamespace(ask_text=AsyncMock(return_value=SimpleNamespace(text="Hello!")))
        answer = AsyncMock(return_value=SimpleNamespace(chat=SimpleNamespace(id=1), message_id=3))
        bot = SimpleNamespace(send_chat_action=AsyncMock())
        with patch.object(Message, "answer", answer):
            await text_handler(
                telegram_message("Hello"),
                bot,
                SimpleNamespace(state=state, users=self.users, requests=ai_router),
            )
        ai_router.ask_text.assert_awaited_once()
        self.assertEqual(ai_router.ask_text.call_args.kwargs["prompt"], "Hello")
        self.assertEqual(answer.call_args.args[0], "Hello!")
