import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from Handlers.accounts import account_callback, activation_message
from Handlers.admin_panel import admin_callback, admin_handler, admin_reason, render
from Handlers.auth import AuthMiddleware
from Handlers.control import ControlFlowMiddleware
from Handlers.flows import ActivationFlow, AdminFlow
from Handlers.models import new_chat_handler, select_model
from Tests.storage_support import StorageTestCase


def fake_message(user_id=999, text="/admin", *, private=True):
    bot = SimpleNamespace(edit_message_text=AsyncMock(), send_message=AsyncMock())
    return SimpleNamespace(from_user=SimpleNamespace(id=user_id), chat=SimpleNamespace(id=user_id, type="private" if private else "group"),
                           message_id=42, text=text, answer=AsyncMock(), edit_text=AsyncMock(), delete=AsyncMock(), bot=bot)


def fake_callback(data, user_id=999, *, private=True):
    return SimpleNamespace(data=data, from_user=SimpleNamespace(id=user_id), message=fake_message(user_id, private=private), answer=AsyncMock())


class AdminPanelTests(StorageTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.add_user(2)
        await self.add_user(999, username="owner")
        self.fsm_storage = MemoryStorage()
        self.fsm = FSMContext(self.fsm_storage, StorageKey(bot_id=1, chat_id=999, user_id=999))
        self.app = SimpleNamespace(admin=self.admin, users=self.users, state=self.state, accounts=self.accounts,
                                   settings=self.settings, notifications=SimpleNamespace(wake=lambda: None))

    async def asyncTearDown(self):
        await self.fsm_storage.close()
        await super().asyncTearDown()

    async def test_admin_command_renders_inline_dashboard_not_command_help(self):
        message = fake_message()
        await admin_handler(message, self.app, self.fsm)
        text = message.answer.call_args.args[0]
        markup = message.answer.call_args.kwargs["reply_markup"]
        self.assertIn("Админ-панель", text)
        self.assertNotIn("/admin block", text)
        labels = [b.text for row in markup.inline_keyboard for b in row]
        for label in ("📊 Статистика", "👥 Пользователи", "🚫 Заблокированные", "⚠️ Модерация", "⭐ Trusted", "🤖 Модели", "📋 Audit Log", "🔄 Обновить"):
            self.assertIn(label, labels)

    async def test_non_admin_callbacks_and_admin_group_callbacks_are_denied(self):
        await self.admin.act(999, "grant_trusted", user_id=2, reason="Trusted is not admin")
        for callback in (fake_callback("adm:home", 2), fake_callback("adm:act:block:999", 2), fake_callback("adm:home", 999, private=False)):
            await admin_callback(callback, self.app, self.fsm)
            self.assertTrue(callback.answer.call_args.kwargs.get("show_alert"))
            callback.message.edit_text.assert_not_awaited()
        self.assertFalse((await self.storage.users.get(999)).is_blocked)

    async def test_user_card_contains_controls_without_typing_ids(self):
        text, markup = await render("adm:user:2", 999, self.app, self.fsm)
        for label in ("Telegram ID", "Регистрация", "Последний визит", "Standard", "Индивидуальный лимит", "AI-вызовы сегодня", "Предупреждения", "Premium Models"):
            self.assertIn(label, text)
        actions = [b.callback_data for row in markup.inline_keyboard for b in row]
        for action in ("block", "restrict", "warn", "clear_warnings", "grant_trusted", "grant_premium"):
            self.assertIn(f"adm:act:{action}:2", actions)

    async def test_reason_confirmation_and_double_click_are_safe(self):
        await admin_callback(fake_callback("adm:act:block:2"), self.app, self.fsm)
        self.assertEqual(await self.fsm.get_state(), AdminFlow.reason.state)
        self.assertFalse((await self.storage.users.get(2)).is_blocked)
        reason = fake_message(text="Reviewed violation")
        await admin_reason(reason, self.app, self.fsm)
        self.assertFalse((await self.storage.users.get(2)).is_blocked)
        data = await self.fsm.get_data()
        route = f"adm:confirm:{data['nonce']}"
        control = ControlFlowMiddleware()

        async def handler(event, data):
            await admin_callback(event, self.app, self.fsm)
        callbacks = [fake_callback(route), fake_callback(route)]
        await asyncio.gather(*[control(handler, cb, {"state": self.fsm}) for cb in callbacks])
        self.assertTrue((await self.storage.users.get(2)).is_blocked)
        self.assertEqual(len(await self.admin.audit(999, 2)), 1)
        self.assertIsNone(await self.fsm.get_state())

    async def test_cancel_and_forged_confirmation_do_not_mutate(self):
        await admin_callback(fake_callback("adm:act:block:2"), self.app, self.fsm)
        await admin_callback(fake_callback("adm:cancel"), self.app, self.fsm)
        await admin_callback(fake_callback("adm:confirm:forged"), self.app, self.fsm)
        self.assertFalse((await self.storage.users.get(2)).is_blocked)
        self.assertEqual(await self.admin.audit(999, 2), [])

    async def test_restriction_and_limit_presets_still_require_reason_and_confirm(self):
        for action, choice, expected in (("restrict", "adm:duration:60", "restricted"), ("set_limit", "adm:limit:100", "allowed")):
            await admin_callback(fake_callback(f"adm:act:{action}:2"), self.app, self.fsm)
            await admin_callback(fake_callback(choice), self.app, self.fsm)
            await admin_reason(fake_message(text="Reviewed"), self.app, self.fsm)
            data = await self.fsm.get_data()
            await admin_callback(fake_callback(f"adm:confirm:{data['nonce']}"), self.app, self.fsm)
            if action == "restrict":
                self.assertEqual((await self.users.access(2)).code, expected)
                await self.admin.act(999, "unrestrict", user_id=2, reason="Next test")
        self.assertEqual((await self.accounts.status(2))["effective_daily_limit"], 100)

    async def test_all_admin_views_paginate_and_callbacks_fit_telegram(self):
        for uid in range(20, 32):
            await self.add_user(uid, username=f"user{uid}")
        app = await self.accounts.apply_trusted(2)
        event = await self.moderation.record(user_id=2, request_id="request", model="gpt_oss_120b", provider="amazon_bedrock",
            category="policy", severity="low", source="input", action="flag", code="test_rule", guardrail="test")
        await self.admin.act(999, "warn", user_id=2, event_id=event, reason="Reviewed")
        routes = ["adm:home", "adm:stats", "adm:users:all:0", "adm:users:all:1", "adm:users:blocked:0",
                  "adm:users:trusted:0", "adm:trusted:0", f"adm:app:{app['id']}", "adm:events:open:0",
                  f"adm:event:{event}", "adm:audit:0", "adm:audit_entry:1", "adm:models:0", "adm:models:1",
                  "adm:user_usage:2:0", "adm:user_events:2:0", "adm:user_audit:2:0"]
        for route in routes:
            text, markup = await render(route, 999, self.app, self.fsm)
            self.assertLess(len(text), 4096)
            self.assertNotIn(self.settings.grok_access_code, text)
            self.assertNotIn(self.settings.premium_activation_code, text)
            for row in markup.inline_keyboard:
                for button in row:
                    self.assertTrue(1 <= len(button.callback_data.encode()) <= 64, button.callback_data)
        first = await self.admin.page(999, "users", 0)
        second = await self.admin.page(999, "users", 1)
        self.assertFalse({u["telegram_user_id"] for u in first.items} & {u["telegram_user_id"] for u in second.items})

    async def test_approval_via_application_buttons_uses_admin_service(self):
        application = await self.accounts.apply_trusted(2)
        await admin_callback(fake_callback(f"adm:app_act:approve_trusted:{application['id']}"), self.app, self.fsm)
        await admin_reason(fake_message(text="Approved by owner"), self.app, self.fsm)
        data = await self.fsm.get_data()
        await admin_callback(fake_callback(f"adm:confirm:{data['nonce']}"), self.app, self.fsm)
        self.assertEqual((await self.accounts.status(2))["trusted_source"], "admin_approval")

    async def test_locked_grok_selection_prompts_code_and_consumes_it_without_chat(self):
        fsm = FSMContext(self.fsm_storage, StorageKey(bot_id=1, chat_id=2, user_id=2))
        callback = fake_callback("model:amazon_bedrock:xAI:grok_4_6", 2)
        await select_model(callback, self.app, fsm)
        self.assertEqual(await fsm.get_state(), ActivationFlow.code.state)
        self.assertIsNone((await self.users.selection((2, 2))).model_key)
        message = fake_message(2, self.settings.grok_access_code)
        await activation_message(message, self.app, fsm)
        message.delete.assert_awaited_once()
        self.assertIsNone(await fsm.get_state())
        self.assertTrue((await self.accounts.status(2))["grok_allowed"])
        self.assertEqual((await self.users.selection((2, 2))).model_key, "grok_4_6")
        self.assertNotIn(self.settings.grok_access_code, str(message.bot.edit_message_text.call_args))
        self.assertFalse(self.state.histories)

    async def test_account_navigation_creates_application_and_never_counts_as_inference(self):
        fsm = FSMContext(self.fsm_storage, StorageKey(bot_id=1, chat_id=2, user_id=2))
        for route in ("acct:trusted", "acct:status", "acct:grok", "acct:apply", "acct:apply"):
            await account_callback(fake_callback(route, 2), self.app, fsm)
        self.assertEqual(len(await self.rows("SELECT * FROM trusted_applications")), 1)
        self.assertEqual((await self.accounts.status(2))["requests_today"], 0)

    async def test_new_chat_does_not_wait_for_regular_ai_event(self):
        control = ControlFlowMiddleware()
        fsm = FSMContext(self.fsm_storage, StorageKey(bot_id=1, chat_id=2, user_id=2))
        started, release = asyncio.Event(), asyncio.Event()
        key = (2, 2)
        old = self.state.snapshot(key)

        async def ai_handler(event, data):
            started.set()
            await release.wait()

        async def reset_handler(event, data):
            await new_chat_handler(event, self.app, fsm)

        task = asyncio.create_task(control(ai_handler, fake_message(2, "Hello"), {"state": fsm}))
        await started.wait()
        await asyncio.wait_for(control(reset_handler, fake_callback("new_chat", 2), {"state": fsm}), timeout=1)
        self.assertFalse(self.state.is_current(old))
        self.assertFalse(task.done())
        release.set()
        await task
