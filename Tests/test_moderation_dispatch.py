from datetime import datetime, timezone
from types import SimpleNamespace

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import EditMessageText
from aiogram.types import Update

from AI.moderation import SAFETY_REFUSAL
from AI.input_moderation import InputModerator
from Handlers.accounts import account_callback
from Handlers.admin_panel import admin_callback, admin_handler, admin_reason
from Handlers.appeals import appeal_callback, appeal_explanation, appeal_fallback
from Handlers.auth import AuthMiddleware
from Handlers.chat import text_handler
from Handlers.control import ControlFlowMiddleware
from Handlers.flows import AdminFlow, AppealFlow
from Handlers.media import document_handler, photo_handler
from Handlers.start import start_handler
from Services.notifications import NotificationService
from Tests.moderation_support import ModerationTestCase
from Tests.telegram_support import LocalTelegramSession
from Tests.test_usage_persistence import completion


class ModerationDispatcherTests(ModerationTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.session = LocalTelegramSession()
        self.bot = Bot(self.settings.bot_token, session=self.session)
        self.dispatcher = Dispatcher(storage=MemoryStorage())
        self.service_instance, self.upstream = self.service([
            completion(finish_reason="content_filter"), completion(refusal="provider private refusal"),
            completion(finish_reason="content_filter"), completion(content="Allowed after appeal")])
        self.notifications = NotificationService(self.storage.notifications, self.bot)
        self.app = SimpleNamespace(settings=self.settings, users=self.users, state=self.state,
                                   requests=self.service_instance, accounts=self.accounts, admin=self.admin,
                                   moderation=self.moderation, notifications=self.notifications)
        self.dispatcher["app_context"] = self.app
        control = ControlFlowMiddleware()
        auth = AuthMiddleware(state=self.state, users=self.users, accounts=self.accounts)
        self.dispatcher.message.outer_middleware(control)
        self.dispatcher.callback_query.outer_middleware(control)
        self.dispatcher.message.outer_middleware(auth)
        self.dispatcher.callback_query.outer_middleware(auth)
        # Fresh routers, same production handlers/order/filters; no polling or network.
        router = Router()
        router.message.register(start_handler, CommandStart())
        router.message.register(admin_handler, Command("admin"))
        router.callback_query.register(admin_callback, F.data.startswith("adm:"))
        router.message.register(admin_reason, AdminFlow.reason, F.text)
        router.callback_query.register(appeal_callback, F.data.startswith("appeal:"))
        router.message.register(appeal_explanation, AppealFlow.explanation, F.text)
        router.message.register(appeal_fallback, AppealFlow.explanation)
        router.callback_query.register(account_callback, F.data.startswith("acct:"))
        router.message.register(photo_handler, F.photo)
        router.message.register(document_handler, F.document)
        router.message.register(text_handler, F.text)
        self.dispatcher.include_router(router)
        self.sequence = 0
        await self.users.select_model((2, 2), "amazon_bedrock", "gpt_oss_120b")

    async def asyncTearDown(self):
        await self.service_instance.close()
        await self.notifications.close()
        await self.dispatcher.storage.close()
        await self.bot.session.close()
        await super().asyncTearDown()

    async def feed(self, user_id=2, *, text=None, callback=None, photo=False):
        self.sequence += 1
        user = {"id": user_id, "is_bot": False, "first_name": "Test"}
        message = {"message_id": self.sequence, "date": int(datetime.now(timezone.utc).timestamp()),
                   "chat": {"id": user_id, "type": "private"}, "from": user}
        if callback is None:
            if photo:
                message["photo"] = [{"file_id": "fake", "file_unique_id": "fake", "width": 2, "height": 2}]
            else:
                message["text"] = text
            payload = {"update_id": self.sequence, "message": message}
        else:
            message["text"] = "Inline panel"
            payload = {"update_id": self.sequence, "callback_query": {
                "id": str(self.sequence), "chat_instance": "local", "from": user, "message": message, "data": callback}}
        await self.dispatcher.feed_update(self.bot, Update.model_validate(payload, context={"bot": self.bot}))

    async def three_real_violations(self):
        for count in range(1, 4):
            await self.feed(text="Example request")
            self.assertEqual(self.session.sent[-2].text, SAFETY_REFUSAL)
            self.assertIn(f"{count}/3", self.session.sent[-1].text)
        self.assertEqual(self.upstream.await_count, 3)

    async def test_third_warning_shows_appeal_button_despite_invalidated_conversation(self):
        await self.three_real_violations()
        notice = self.session.sent[-1]
        self.assertIn("Ваш доступ заблокирован", notice.text)
        self.assertEqual(notice.reply_markup.inline_keyboard[0][0].text, "📨 Обжаловать блокировку")
        self.assertFalse(any(self.state.histories.values()))

    async def test_blocked_user_can_appeal_but_no_other_control_or_inference(self):
        await self.three_real_violations()
        block = await self.moderation.block_for_user(2)
        for text in ("/start", "Normal inference", "/admin", "/menu"):
            await self.feed(text=text)
            self.assertIn("заблокирован", self.session.sent[-1].text)
        for route in ("acct:trusted", "acct:grok", "acct:premium", "acct:apply", "main_menu", "new_chat",
                      "model:amazon_bedrock:OpenAI:gpt_oss_120b"):
            await self.feed(callback=route)
        await self.feed(photo=True)
        self.assertEqual(self.upstream.await_count, 3)
        self.assertEqual((await self.accounts.status(2))["requests_today"], 0)
        await self.feed(callback=f"appeal:open:{block['id']}")
        await self.feed(text="I believe this was misclassified.")
        self.assertEqual(len(await self.rows("SELECT * FROM moderation_appeals")), 1)
        self.assertIn("ожидает решения", self.session.sent[-1].text)
        await self.feed(callback=f"appeal:open:{block['id']}")
        await self.feed(text="Duplicate explanation")
        self.assertEqual(len(await self.rows("SELECT * FROM moderation_appeals")), 1)
        self.assertEqual(self.upstream.await_count, 3)

    async def test_complete_appeal_approval_dispatch_notification_and_restored_access(self):
        await self.three_real_violations()
        block = await self.moderation.block_for_user(2)
        await self.feed(callback=f"appeal:open:{block['id']}")
        await self.feed(text="Please overturn the third finding.")
        appeal = (await self.rows("SELECT * FROM moderation_appeals"))[0]
        await self.feed(999, text="/admin")
        self.assertIn("📨 Апелляции: 1", self.session.sent[-1].text)
        await self.feed(999, callback="adm:appeals:pending:0")
        await self.feed(999, callback=f"adm:appeal:{appeal['id']}")
        await self.feed(999, callback=f"adm:appeal_act:approve_appeal:{appeal['id']}")
        await self.feed(999, text="Confirmed mistaken signal")
        fsm = self.dispatcher.fsm.get_context(bot=self.bot, chat_id=999, user_id=999)
        data = await fsm.get_data()
        # A non-admin with an actual admin nonce still cannot approve.
        await self.feed(3, callback=f"adm:confirm:{data['nonce']}")
        self.assertEqual(await self.storage.moderation.active_warnings(2), 3)
        await self.feed(999, callback=f"adm:confirm:{data['nonce']}")
        await self.notifications.flush()
        self.assertIn("удовлетворена", self.session.sent[-1].text)
        self.assertEqual(self.session.sent[-1].chat.id, 2)
        self.assertEqual(await self.storage.moderation.active_warnings(2), 2)
        self.assertTrue((await self.users.access(2)).allowed)
        self.assertTrue(any(isinstance(method, EditMessageText) for method in self.session.methods))
        await self.feed(text="Allowed request")
        self.assertEqual(self.session.sent[-1].text, "Allowed after appeal")
        self.assertEqual((await self.accounts.status(2))["requests_today"], 1)

    async def test_start_and_admin_menus_never_consume_inference_quota(self):
        for _ in range(3):
            await self.feed(text="/start")
            await self.feed(999, text="/admin")
            await self.feed(callback="acct:status")
        self.upstream.assert_not_awaited()
        self.assertEqual(await self.rows("SELECT * FROM usage_requests"), [])
        self.assertEqual((await self.accounts.status(2))["requests_today"], 0)

    async def test_gemma_input_contract_dispatch_blocks_before_chat_and_opens_appeal(self):
        from Tests.test_input_moderation import verdict
        self.upstream.side_effect = [verdict(True), verdict(True), verdict(True)]
        self.moderation.input_moderator = InputModerator(self.service_instance.router._client,
                                                        self.service_instance.router._semaphore)
        await self.users.select_model((2, 2), "amazon_bedrock", "gemma_3_12b_it")
        for count in range(1, 4):
            await self.feed(text="Как украсть пароль от чужого Telegram-аккаунта?")
            self.assertEqual(self.session.sent[-2].text, SAFETY_REFUSAL)
            self.assertIn(f"{count}/3", self.session.sent[-1].text)
        self.assertTrue(all(call.kwargs["model"] == "openai.gpt-oss-safeguard-20b" for call in self.upstream.call_args_list))
        self.assertEqual(await self.rows("SELECT * FROM usage_attempts WHERE purpose = 'product'"), [])
        block = await self.moderation.block_for_user(2)
        await self.feed(callback=f"appeal:open:{block['id']}")
        await self.feed(text="Review the classification, please.")
        self.assertEqual(len(await self.rows("SELECT * FROM moderation_appeals")), 1)
        self.assertEqual((await self.accounts.status(2))["requests_today"], 0)
