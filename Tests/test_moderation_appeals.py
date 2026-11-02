import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from Handlers.admin_panel import admin_callback, admin_reason, render
from Handlers.appeals import appeal_callback, appeal_explanation
from Handlers.flows import AppealFlow
from Services.notifications import NotificationService
from Storage.errors import StorageError
from Tests.moderation_support import ModerationTestCase
from Tests.test_admin_panel import fake_callback, fake_message


class ModerationAppealTests(ModerationTestCase):
    async def test_blocked_user_can_create_a_linked_persistent_appeal(self):
        outcome = await self.ban()
        result = await self.submit_appeal()
        appeal = await self.moderation.appeal_for_user(2, result["id"])
        self.assertEqual(appeal["status"], "pending")
        self.assertEqual((appeal["moderation_event_id"], appeal["warning_id"], appeal["sanction_id"]),
                         (outcome.event_id, outcome.warning_id, outcome.sanction_id))
        self.assertIsNone(appeal["reviewed_by"])
        self.assertEqual((await self.users.access(2)).code, "blocked")

    async def test_duplicate_pending_appeals_are_atomic_and_do_not_duplicate_notifications(self):
        await self.ban()
        results = await asyncio.gather(*[self.submit_appeal() for _ in range(8)])
        self.assertEqual(sum(r["created"] for r in results), 1)
        self.assertEqual(len(await self.rows("SELECT * FROM moderation_appeals")), 1)
        self.assertEqual(len(await self.rows("SELECT * FROM notification_outbox WHERE kind = 'appeal_pending'")), 1)

    async def test_foreign_block_and_appeal_references_are_rejected(self):
        block = await self.ban()
        await self.add_user(3)
        with self.assertRaises(ValueError):
            await self.moderation.appeal(3, block.sanction_id, "Forged owner")
        appeal = await self.submit_appeal()
        self.assertEqual(await self.moderation.appeal_for_user(3, appeal["id"]), {})
        self.assertEqual(await self.moderation.block_for_user(3, block.sanction_id), {})
        with self.assertRaises(ValueError):
            await self.admin.act(999, "approve_appeal", user_id=3, appeal_id=appeal["id"], reason="Wrong owner")

    async def test_manual_block_has_no_automatic_appeal(self):
        await self.admin.act(999, "block", user_id=2, reason="Manual ban")
        self.assertEqual(await self.moderation.block_for_user(2), {})
        with self.assertRaises(ValueError):
            await self.moderation.appeal(2, 123, "No automatic decision")

    async def test_approve_revokes_only_linked_warning_and_unblocks_at_two(self):
        outcome = await self.ban()
        appeal = await self.submit_appeal()
        result = await self.admin.act(999, "approve_appeal", appeal_id=appeal["id"], reason="Misclassified request")
        self.assertEqual(await self.storage.moderation.active_warnings(2), 2)
        self.assertTrue((await self.users.access(2)).allowed)
        warnings = await self.rows("SELECT id, status FROM user_warnings ORDER BY id")
        self.assertEqual([row["status"] for row in warnings], ["active", "active", "revoked"])
        self.assertEqual(warnings[-1]["id"], outcome.warning_id)
        reviewed = await self.moderation.appeal_for_user(2, appeal["id"])
        self.assertEqual(reviewed["status"], "approved")
        self.assertEqual((reviewed["reviewed_by"], reviewed["admin_action_id"]), (999, result.audit_id))
        self.assertEqual(len(await self.rows("SELECT * FROM moderation_events")), 3)
        self.assertEqual((await self.admin.detail(999, "events", outcome.event_id))["review_status"], "dismissed")
        self.assertIsNotNone((await self.rows("SELECT revoked_at FROM moderation_sanctions"))[0]["revoked_at"])
        audit = (await self.admin.audit(999, 2))[0]
        self.assertEqual(audit["resource_type"], "moderation_appeal")
        self.assertIn('"active_warning_count":2', audit["metadata_json"])
        # The next actual violation is warning 3 again, and creates a new ban.
        again = await self.confirm()
        self.assertEqual(again.active_warning_count, 3)
        self.assertNotEqual(again.sanction_id, outcome.sanction_id)

    async def test_approve_never_removes_manual_admin_block(self):
        await self.ban()
        appeal = await self.submit_appeal()
        await self.admin.act(999, "block", user_id=2, reason="Independent manual decision")
        before = await self.rows("SELECT is_blocked, blocked_at, blocked_by, block_reason, block_source FROM users WHERE telegram_user_id = 2")
        await self.admin.act(999, "approve_appeal", appeal_id=appeal["id"], reason="Overturn automated finding")
        after = await self.rows("SELECT is_blocked, blocked_at, blocked_by, block_reason, block_source FROM users WHERE telegram_user_id = 2")
        self.assertEqual(before, after)
        self.assertEqual((await self.users.access(2)).code, "blocked")
        self.assertEqual(await self.moderation.block_for_user(2), {})
        self.assertEqual(await self.storage.moderation.active_warnings(2), 2)

    async def test_manual_unblock_cannot_remove_automatic_block(self):
        await self.ban()
        await self.admin.act(999, "unblock", user_id=2, reason="Manual control only")
        self.assertEqual((await self.users.access(2)).code, "blocked")

    async def test_reject_preserves_block_warnings_and_history(self):
        await self.ban()
        appeal = await self.submit_appeal()
        await self.admin.act(999, "reject_appeal", appeal_id=appeal["id"], reason="Confirmed policy violation")
        self.assertEqual((await self.moderation.appeal_for_user(2, appeal["id"]))["status"], "rejected")
        self.assertEqual(await self.storage.moderation.active_warnings(2), 3)
        self.assertEqual((await self.users.access(2)).code, "blocked")
        self.assertEqual(len(await self.rows("SELECT * FROM moderation_events")), 3)
        # One reviewed appeal per sanction; repeated clicks cannot spam reviews.
        self.assertFalse((await self.submit_appeal())["created"])

    async def test_review_is_admin_only_and_repeated_review_does_not_mutate(self):
        await self.ban()
        appeal = await self.submit_appeal()
        for action in ("approve_appeal", "reject_appeal"):
            with self.assertRaises(PermissionError):
                await self.admin.act(2, action, appeal_id=appeal["id"], reason="Forged approval")
        await self.admin.act(999, "approve_appeal", appeal_id=appeal["id"], reason="Approved")
        with self.assertRaises(ValueError):
            await self.admin.act(999, "reject_appeal", appeal_id=appeal["id"], reason="Already reviewed")
        self.assertEqual(len(await self.admin.audit(999, 2)), 1)

    async def test_audit_failure_rolls_back_entire_appeal_review(self):
        await self.ban()
        appeal = await self.submit_appeal()
        await self.storage.database.run(lambda c: c.execute(
            "CREATE TRIGGER review_audit_failure BEFORE INSERT ON admin_audit_log BEGIN SELECT RAISE(ABORT, 'test'); END"), write=True)
        with self.assertRaises(StorageError):
            await self.admin.act(999, "approve_appeal", appeal_id=appeal["id"], reason="Must roll back")
        self.assertEqual((await self.moderation.appeal_for_user(2, appeal["id"]))["status"], "pending")
        self.assertEqual(await self.storage.moderation.active_warnings(2), 3)
        self.assertEqual((await self.users.access(2)).code, "blocked")

    async def test_three_remaining_warnings_cannot_be_bypassed_by_appeal(self):
        original = await self.ban()
        # Simulate a provider response from a different chat already in flight.
        await self.confirm()
        appeal = await self.submit_appeal()
        await self.admin.act(999, "approve_appeal", appeal_id=appeal["id"], reason="One finding overturned")
        self.assertEqual(await self.storage.moderation.active_warnings(2), 3)
        block = await self.moderation.block_for_user(2)
        self.assertNotEqual(block["id"], original.sanction_id)
        self.assertNotEqual(block["warning_id"], original.warning_id)
        self.assertEqual((await self.users.access(2)).code, "blocked")

    async def test_pending_appeals_warns_admin_and_review_notifies_user_for_both_decisions(self):
        bot = SimpleNamespace(send_message=AsyncMock())
        notifications = NotificationService(self.storage.notifications, bot)
        for uid, action, text in ((2, "approve_appeal", "удовлетворена"), (3, "reject_appeal", "отклонена")):
            if uid == 3:
                await self.add_user(uid)
            await self.ban(uid)
            appeal = await self.submit_appeal(uid)
            await notifications.flush()
            self.assertEqual(bot.send_message.call_args.args[0], 999)
            self.assertIn(f"adm:appeal:{appeal['id']}", str(bot.send_message.call_args))
            await self.admin.act(999, action, appeal_id=appeal["id"], reason="Review decision")
            await notifications.flush()
            self.assertEqual(bot.send_message.call_args.args[0], uid)
            self.assertIn(text, bot.send_message.call_args.args[1])
        self.assertEqual(bot.send_message.await_count, 4)
        await notifications.flush()
        self.assertEqual(bot.send_message.await_count, 4)

    async def test_failed_user_notification_is_durable_and_retried_without_re_review(self):
        await self.ban()
        appeal = await self.submit_appeal()
        await self.admin.act(999, "approve_appeal", appeal_id=appeal["id"], reason="Approved")
        bot = SimpleNamespace(send_message=AsyncMock(side_effect=[RuntimeError("private provider body"), None]))
        notifications = NotificationService(self.storage.notifications, bot)
        await notifications.flush()
        row = (await self.rows("SELECT * FROM notification_outbox WHERE kind = 'appeal_approved'"))[0]
        self.assertEqual((row["status"], row["attempts"], row["error_code"]), ("pending", 1, "RuntimeError"))
        await self.reopen()
        await self.storage.database.run(lambda c: c.execute("UPDATE notification_outbox SET next_attempt_at = created_at"), write=True)
        notifications = NotificationService(self.storage.notifications, bot)
        await notifications.flush()
        self.assertEqual(bot.send_message.await_count, 2)
        self.assertEqual(len(await self.admin.audit(999, 2)), 1)

    async def test_restart_preserves_warnings_ban_appeal_but_not_conversation(self):
        outcome = await self.ban()
        appeal = await self.submit_appeal()
        self.state.history_for((10, 2)).append({"role": "user", "content": "private RAM-only conversation"})
        await self.reopen()
        self.assertEqual(await self.storage.moderation.active_warnings(2), 3)
        self.assertEqual((await self.moderation.block_for_user(2))["id"], outcome.sanction_id)
        self.assertEqual((await self.moderation.appeal_for_user(2, appeal["id"]))["status"], "pending")
        self.assertEqual(self.state.histories, {})
        dump = await self.storage.database.run(lambda c: "\n".join(c.iterdump()))
        self.assertNotIn("private RAM-only conversation", dump)

    async def test_appeal_text_and_admin_reason_redact_secrets_and_are_bounded(self):
        await self.ban()
        secret_text = " ".join((self.settings.bot_token, self.settings.grok_access_code, self.settings.premium_activation_code))
        appeal = await self.submit_appeal(explanation="Please review " + secret_text)
        await self.admin.act(999, "approve_appeal", appeal_id=appeal["id"], reason="Review " + secret_text)
        dump = await self.storage.database.run(lambda c: "\n".join(c.iterdump()))
        for secret in (self.settings.bot_token, self.settings.grok_access_code, self.settings.premium_activation_code):
            self.assertNotIn(secret, dump)
        self.assertIn("[redacted]", dump)
        for explanation in ("", "x" * 1001):
            with self.assertRaises(ValueError):
                await self.moderation.appeal(2, 1, explanation)

    async def test_retention_keeps_appeal_decision_event_warning_and_admin_audit(self):
        await self.ban()
        appeal = await self.submit_appeal()
        await self.admin.act(999, "approve_appeal", appeal_id=appeal["id"], reason="Overturned")
        for table in ("moderation_events", "admin_audit_log"):
            await self.storage.database.run(lambda c: c.execute(f"UPDATE {table} SET timestamp = '2000-01-01'"), write=True)
        await self.storage.purge(usage_before="2020-01-01", moderation_before="2020-01-01", audit_before="2020-01-01")
        self.assertEqual(len(await self.rows("SELECT * FROM moderation_events")), 3)
        self.assertEqual(len(await self.rows("SELECT * FROM user_warnings")), 3)
        self.assertEqual(len(await self.admin.audit(999, 2)), 1)


class AppealPanelTests(ModerationTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.memory = MemoryStorage()
        self.admin_fsm = FSMContext(self.memory, StorageKey(bot_id=1, chat_id=999, user_id=999))
        self.user_fsm = FSMContext(self.memory, StorageKey(bot_id=1, chat_id=2, user_id=2))
        self.app = SimpleNamespace(admin=self.admin, users=self.users, state=self.state, moderation=self.moderation,
                                   accounts=self.accounts, settings=self.settings, notifications=SimpleNamespace(wake=lambda: None))

    async def asyncTearDown(self):
        await self.memory.close()
        await super().asyncTearDown()

    async def test_appeal_ui_accepts_explanation_without_inference(self):
        block = await self.ban()
        await appeal_callback(fake_callback(f"appeal:open:{block.sanction_id}", 2), self.app, self.user_fsm)
        self.assertEqual(await self.user_fsm.get_state(), AppealFlow.explanation.state)
        message = fake_message(2, "Please review this decision")
        await appeal_explanation(message, self.app, self.user_fsm)
        self.assertIsNone(await self.user_fsm.get_state())
        self.assertEqual(len(await self.rows("SELECT * FROM moderation_appeals")), 1)
        self.assertEqual((await self.accounts.status(2))["requests_today"], 0)
        self.assertEqual((await self.users.access(2)).code, "blocked")

    async def test_user_cannot_open_foreign_appeal_or_forge_approve_callback(self):
        block = await self.ban()
        appeal = await self.submit_appeal()
        for route in (f"appeal:open:{block.sanction_id}", f"appeal:status:{appeal['id']}",
                      f"appeal:approve:{appeal['id']}"):
            callback = fake_callback(route, 3)
            await appeal_callback(callback, self.app, self.user_fsm)
            self.assertIn("недоступна", callback.answer.call_args.args[0])
        for route in (f"adm:appeal:{appeal['id']}", "adm:appeals:pending:0",
                      f"adm:appeal_act:approve_appeal:{appeal['id']}", "adm:confirm:forged"):
            callback = fake_callback(route, 2)
            await admin_callback(callback, self.app, self.user_fsm)
            self.assertIn("только для администратора", callback.answer.call_args.args[0])
        self.assertEqual(await self.storage.moderation.active_warnings(2), 3)

    async def test_admin_appeal_buttons_use_reason_confirmation_and_recheck_identity(self):
        await self.ban()
        appeal = await self.submit_appeal()
        await admin_callback(fake_callback(f"adm:appeal_act:approve_appeal:{appeal['id']}"), self.app, self.admin_fsm)
        await admin_reason(fake_message(text="Overturned after review"), self.app, self.admin_fsm)
        data = await self.admin_fsm.get_data()
        self.admin.admin_ids = frozenset()
        denied = fake_callback(f"adm:confirm:{data['nonce']}")
        await admin_callback(denied, self.app, self.admin_fsm)
        self.assertEqual(await self.storage.moderation.active_warnings(2), 3)
        self.admin.admin_ids = frozenset({999})
        await admin_callback(fake_callback(f"adm:confirm:{data['nonce']}"), self.app, self.admin_fsm)
        self.assertEqual(await self.storage.moderation.active_warnings(2), 2)

    async def test_pending_count_pagination_and_appeal_card_with_history(self):
        for uid in range(2, 12):
            if uid != 2:
                await self.add_user(uid)
            await self.ban(uid)
            await self.submit_appeal(uid, explanation="Review <b>literal text</b>")
        self.assertEqual((await self.admin.dashboard(999))["pending_appeals"], 10)
        first = await self.admin.page(999, "appeals", 0, filter_value="pending")
        second = await self.admin.page(999, "appeals", 1, filter_value="pending")
        self.assertFalse({a["id"] for a in first.items} & {a["id"] for a in second.items})
        aid = first.items[0]["id"]
        for route in ("adm:home", "adm:appeals:pending:0", "adm:appeals:pending:1", f"adm:appeal:{aid}", f"adm:appeal_history:{aid}:0"):
            text, markup = await render(route, 999, self.app, self.admin_fsm)
            self.assertLess(len(text), 4096)
            self.assertTrue(all(len(button.callback_data.encode()) <= 64 for row in markup.inline_keyboard for button in row))
        text, markup = await render(f"adm:appeal:{aid}", 999, self.app, self.admin_fsm)
        for required in ("Telegram ID", "gpt_oss_120b", "amazon_bedrock", "3/3", "Последние события", "&lt;b&gt;"):
            self.assertIn(required, text)
        self.assertNotIn("Категория: unknown", text)
        labels = {b.text for row in markup.inline_keyboard for b in row}
        self.assertTrue({"✅ Удовлетворить", "❌ Отклонить", "👤 Пользователь", "📋 История модерации", "⬅️ Назад"} <= labels)
