import asyncio
import os
import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiogram.types import Message

from Handlers.auth import AuthMiddleware
from Services.access import AccountService
from Services.notifications import NotificationService
from Storage.migrations import MIGRATIONS, migrate
from Storage.models import AccessDeniedError, utc_timestamp
from Tests.storage_support import StorageTestCase
from Tests.test_ui_english import telegram_message
from config import Settings


class AccountAccessTests(StorageTestCase):
    async def test_first_message_registers_standard_without_password(self):
        handler = AsyncMock(return_value="handled")
        middleware = AuthMiddleware(state=self.state, users=self.users, accounts=self.accounts)
        result = await middleware(handler, telegram_message("Hello"), {})
        self.assertEqual(result, "handled")
        handler.assert_awaited_once()
        status = await self.accounts.status(2)
        self.assertEqual(status["tier"], "STANDARD")
        self.assertEqual(status["effective_daily_limit"], 50)
        self.assertFalse(status["grok_allowed"])
        self.assertEqual(status["requests_today"], 0)

    async def test_ban_dominates_trusted_grok_and_code_activation(self):
        await self.add_user()
        await self.admin.act(999, "grant_trusted", user_id=2, reason="Granted")
        await self.admin.act(999, "grant_grok", user_id=2, reason="Granted")
        await self.admin.act(999, "block", user_id=2, reason="Ban")
        await self.reopen()
        self.assertEqual((await self.users.access(2)).code, "blocked")
        for purpose, code in (("grok", self.settings.grok_access_code), ("premium", self.settings.premium_activation_code)):
            with self.assertRaises(AccessDeniedError):
                await self.accounts.activate(2, purpose, code)
        handler = AsyncMock()
        with patch.object(Message, "answer", AsyncMock()):
            await AuthMiddleware(state=self.state, users=self.users)(handler, telegram_message("/start"), {})
        handler.assert_not_awaited()

    async def test_restriction_and_revoked_status_are_not_bypassed(self):
        await self.add_user()
        await self.admin.act(999, "restrict", user_id=2, reason="Security", minutes=60)
        with self.assertRaises(AccessDeniedError):
            await self.accounts.activate(2, "premium", self.settings.premium_activation_code)
        await self.admin.act(999, "unrestrict", user_id=2, reason="Done")
        await self.storage.database.run(lambda c: c.execute("UPDATE users SET status = 'revoked' WHERE telegram_user_id = 2").rowcount, write=True)
        await self.users.observe(telegram_message("Hello").from_user)
        self.assertEqual((await self.users.access(2)).code, "unauthorized")

    async def test_grok_starts_locked_and_selection_does_not_change_on_denial(self):
        await self.add_user()
        await self.users.select_model((1, 2), "amazon_bedrock", "gpt_oss_120b")
        with self.assertRaises(AccessDeniedError) as caught:
            await self.users.select_model((1, 2), "amazon_bedrock", "grok_4_6")
        self.assertEqual(caught.exception.decision.code, "premium_locked")
        self.assertEqual((await self.users.selection((1, 2))).model_key, "gpt_oss_120b")

    async def test_wrong_code_does_not_grant_permission_or_ban(self):
        await self.add_user()
        result = await self.accounts.activate(2, "grok", "wrong")
        self.assertEqual(result.status, "invalid")
        self.assertFalse((await self.accounts.status(2))["grok_allowed"])
        self.assertFalse((await self.storage.users.get(2)).is_blocked)

    async def test_correct_grok_code_persists_through_restart_and_code_rotation(self):
        await self.add_user()
        result = await self.accounts.activate(2, "grok", self.settings.grok_access_code)
        self.assertEqual(result.status, "activated")
        await self.reopen()
        rotated = AccountService(self.storage.accounts, replace(self.settings, grok_access_code="changed-test-code"))
        self.assertTrue((await rotated.status(2))["grok_allowed"])
        await self.users.select_model((1, 2), "amazon_bedrock", "grok_4_6")
        self.assertEqual((await rotated.status(2))["tier"], "STANDARD")
        self.assertEqual((await rotated.status(2))["grok_source"], "access_code")

    async def test_activation_cooldown_survives_restart_and_expires_without_ban(self):
        await self.add_user()
        for _ in range(5):
            result = await self.accounts.activate(2, "grok", "wrong")
        self.assertEqual(result.status, "cooldown")
        await self.reopen()
        self.assertEqual((await self.accounts.activate(2, "grok", self.settings.grok_access_code)).status, "cooldown")
        future = utc_timestamp(datetime.now(timezone.utc) + timedelta(minutes=16))
        with patch("Services.access.utc_timestamp", return_value=future):
            self.assertEqual((await self.accounts.activate(2, "grok", self.settings.grok_access_code)).status, "activated")
        self.assertFalse((await self.storage.users.get(2)).is_blocked)

    async def test_premium_and_grok_have_independent_cooldowns_and_permissions(self):
        await self.add_user()
        for _ in range(5):
            await self.accounts.activate(2, "grok", "wrong")
        result = await self.accounts.activate(2, "premium", self.settings.premium_activation_code)
        self.assertEqual(result.status, "activated")
        status = await self.accounts.status(2)
        self.assertEqual(status["tier"], "TRUSTED")
        self.assertEqual(status["effective_daily_limit"], 500)
        self.assertFalse(status["grok_allowed"])

    async def test_disabled_code_does_not_activate(self):
        await self.add_user()
        disabled = AccountService(self.storage.accounts, replace(self.settings, grok_access_code=""))
        self.assertEqual((await disabled.activate(2, "grok", "anything")).status, "disabled")

    async def test_only_one_pending_application_is_created_concurrently(self):
        await self.add_user()
        results = await asyncio.gather(*[self.accounts.apply_trusted(2) for _ in range(5)] )
        self.assertEqual(sum(r["created"] for r in results), 1)
        self.assertEqual(len({r["id"] for r in results}), 1)
        self.assertEqual(len(await self.rows("SELECT * FROM trusted_applications")), 1)
        self.assertEqual(len(await self.rows("SELECT * FROM notification_outbox")), 1)
        self.assertEqual((await self.accounts.status(2))["tier"], "STANDARD")

    async def test_approval_is_atomic_audited_and_notifies_user(self):
        await self.add_user()
        application = await self.accounts.apply_trusted(2)
        bot = SimpleNamespace(send_message=AsyncMock())
        notifier = NotificationService(self.storage.notifications, bot)
        await notifier.flush()
        self.assertEqual(bot.send_message.call_args.args[0], 999)
        result = await self.admin.act(999, "approve_trusted", application_id=application["id"], reason="Reviewed")
        await self.reopen()
        status = await self.accounts.status(2)
        self.assertEqual(status["tier"], "TRUSTED")
        self.assertEqual(status["trusted_source"], "admin_approval")
        self.assertEqual(status["application"]["admin_action_id"], result.audit_id)
        notifier = NotificationService(self.storage.notifications, bot)
        await notifier.flush()
        self.assertEqual(bot.send_message.call_args.args[0], 2)
        with self.assertRaises(ValueError):
            await self.admin.act(999, "approve_trusted", application_id=application["id"], reason="Duplicate")
        self.assertEqual(len(await self.admin.audit(999, 2)), 1)

    async def test_rejection_is_persistent_and_notifies_without_grant(self):
        await self.add_user()
        application = await self.accounts.apply_trusted(2)
        await self.admin.act(999, "reject_trusted", application_id=application["id"], reason="Not eligible")
        await self.reopen()
        status = await self.accounts.status(2)
        self.assertEqual(status["tier"], "STANDARD")
        self.assertEqual(status["application"]["status"], "rejected")
        bot = SimpleNamespace(send_message=AsyncMock())
        await NotificationService(self.storage.notifications, bot).flush()
        bot.send_message.assert_awaited_once()
        self.assertEqual(bot.send_message.call_args.args[0], 2)
        self.assertIn("отклонена", bot.send_message.call_args.args[1])

    async def test_notification_failure_retries_from_persistent_queue(self):
        await self.add_user()
        application = await self.accounts.apply_trusted(2)
        await self.admin.act(999, "approve_trusted", application_id=application["id"], reason="Approved")
        bot = SimpleNamespace(send_message=AsyncMock(side_effect=RuntimeError("temporary transport failure")))
        await NotificationService(self.storage.notifications, bot).flush()
        rows = await self.rows("SELECT * FROM notification_outbox WHERE kind = 'trusted_approved'")
        self.assertEqual(rows[0]["status"], "pending")
        self.assertEqual(rows[0]["attempts"], 1)
        await self.reopen()
        bot.send_message = AsyncMock()
        later = utc_timestamp(datetime.now(timezone.utc) + timedelta(minutes=2))
        with patch("Services.notifications.utc_timestamp", return_value=later):
            await NotificationService(self.storage.notifications, bot).flush()
        bot.send_message.assert_awaited_once()
        self.assertEqual((await self.rows("SELECT status FROM notification_outbox WHERE kind = 'trusted_approved'"))[0]["status"], "sent")

    async def test_premium_activation_records_source_and_resolves_pending_application(self):
        await self.add_user()
        await self.accounts.apply_trusted(2)
        await self.accounts.activate(2, "premium", self.settings.premium_activation_code)
        await self.reopen()
        status = await self.accounts.status(2)
        self.assertEqual(status["trusted_source"], "premium_code")
        self.assertEqual(status["application"]["status"], "approved")
        self.assertFalse(status["grok_allowed"])
        dump = await self.storage.database.run(lambda c: "\n".join(c.iterdump()))
        self.assertNotIn(self.settings.premium_activation_code, dump)
        self.assertNotIn(self.settings.grok_access_code, dump)

    async def test_admin_grant_revoke_trusted_preserves_grok(self):
        await self.add_user()
        await self.accounts.activate(2, "grok", self.settings.grok_access_code)
        await self.admin.act(999, "grant_trusted", user_id=2, reason="Manual")
        self.assertEqual((await self.accounts.status(2))["trusted_source"], "manual_admin")
        await self.admin.act(999, "revoke_trusted", user_id=2, reason="Revoked")
        await self.reopen()
        status = await self.accounts.status(2)
        self.assertEqual(status["tier"], "STANDARD")
        self.assertTrue(status["grok_allowed"])
        self.assertIsNone(status["trusted_source"])

    async def test_admin_grant_revoke_grok_preserves_trusted_and_audit(self):
        await self.add_user()
        await self.accounts.activate(2, "premium", self.settings.premium_activation_code)
        await self.admin.act(999, "grant_grok", user_id=2, reason="Manual")
        self.assertTrue((await self.accounts.status(2))["grok_allowed"])
        await self.admin.act(999, "revoke_grok", user_id=2, reason="Revoked")
        await self.reopen()
        status = await self.accounts.status(2)
        self.assertEqual(status["tier"], "TRUSTED")
        self.assertFalse(status["grok_allowed"])
        self.assertEqual([a["action"] for a in await self.admin.audit(999, 2)], ["revoke_grok", "grant_grok"])

    async def test_individual_limit_overrides_both_tiers_and_can_be_reset(self):
        await self.add_user()
        await self.admin.act(999, "set_limit", user_id=2, daily_request_limit=12, reason="Individual")
        await self.admin.act(999, "grant_trusted", user_id=2, reason="Trusted")
        self.assertEqual((await self.accounts.status(2))["effective_daily_limit"], 12)
        await self.admin.act(999, "reset_limit", user_id=2, reason="Use tier")
        self.assertEqual((await self.accounts.status(2))["effective_daily_limit"], 500)
        await self.admin.act(999, "revoke_trusted", user_id=2, reason="Standard")
        self.assertEqual((await self.accounts.status(2))["effective_daily_limit"], 50)

    async def test_model_change_clears_context_and_restart_only_restores_selection(self):
        await self.add_user()
        key = (1, 2)
        await self.users.select_model(key, "amazon_bedrock", "gpt_oss_120b")
        self.state.history_for(key).append({"role": "user", "content": "ephemeral-test-marker"})
        old = self.state.snapshot(key)
        await self.users.select_model(key, "amazon_bedrock", "deepseek_v3_1")
        self.assertFalse(self.state.is_current(old))
        self.assertNotIn(key, self.state.histories)
        self.state.history_for(key).append({"role": "user", "content": "ephemeral-test-marker"})
        await self.reopen()
        self.assertFalse(self.state.histories)
        self.assertEqual((await self.users.selection(key)).model_key, "deepseek_v3_1")
        dump = await self.storage.database.run(lambda c: "\n".join(c.iterdump()))
        self.assertNotIn("ephemeral-test-marker", dump)

    async def test_known_code_outside_fsm_is_deleted_and_never_reaches_chat(self):
        middleware = AuthMiddleware(state=self.state, users=self.users, accounts=self.accounts)
        handler = AsyncMock()
        with patch.object(Message, "delete", AsyncMock()) as delete, patch.object(Message, "answer", AsyncMock()):
            await middleware(handler, telegram_message(self.settings.grok_access_code), {})
        delete.assert_awaited_once()
        handler.assert_not_awaited()
        self.assertFalse((await self.accounts.status(2))["grok_allowed"])

    def test_settings_no_longer_require_common_password_and_hide_codes_in_repr(self):
        env = {"BOT_TOKEN": "123:fake", "OPENAI_API_KEY": "fake", "OPENAI_BASE_URL": "https://example.test/v1",
               "AIS_API_KEY": "fake", "AIS_API_BASE_URL": "https://ais.example/v1",
               "TAVILY_API_KEY": "fake", "GROK_ACCESS_CODE": "private-grok", "PREMIUM_ACTIVATION_CODE": "private-premium"}
        with patch.dict(os.environ, env, clear=True), patch("config.load_dotenv"):
            settings = Settings.load()
        self.assertEqual((settings.standard_daily_limit, settings.trusted_daily_limit), (50, 500))
        self.assertNotIn("private-grok", repr(settings))
        self.assertNotIn("private-premium", repr(settings))

    def test_v1_to_v2_preserves_control_data_and_v1_checksum(self):
        path = self.path.parent / "legacy.sqlite3"
        with closing(sqlite3.connect(path, isolation_level=None)) as c:
            c.execute("PRAGMA foreign_keys = ON")
            migrate(c, MIGRATIONS[:1])
            timestamp = "2026-10-01T00:00:00.000000+00:00"
            c.execute("INSERT INTO users (telegram_user_id, created_at, status) VALUES (999, ?, 'authorized')", (timestamp,))
            c.execute("INSERT INTO users (telegram_user_id, username, created_at, status, is_blocked, blocked_at, blocked_by, block_reason) "
                      "VALUES (2, 'existing', ?, 'authorized', 1, ?, 999, 'existing ban')", (timestamp, timestamp))
            c.execute("INSERT INTO user_settings VALUES (2, 1, 'grok_4_6', 'amazon_bedrock', ?)", (timestamp,))
            c.execute("INSERT INTO user_limits VALUES (2, 7, ?, 999)", (timestamp,))
            c.execute("INSERT INTO admin_audit_log (timestamp, admin_id, action, target_user_id, resource_type, reason) "
                      "VALUES (?, 999, 'block', 2, 'user', 'existing ban')", (timestamp,))
            c.execute("INSERT INTO moderation_events (timestamp, telegram_user_id, request_id, category, severity, source, action, code, guardrail, admin_action_id) "
                      "VALUES (?, 2, 'legacy-request', 'policy', 'low', 'input', 'flag', 'policy_test', 'test', 1)", (timestamp,))
            c.execute("INSERT INTO user_warnings (telegram_user_id, created_at, reason, created_by, admin_action_id, moderation_event_id) "
                      "VALUES (2, ?, 'existing warning', 999, 1, 1)", (timestamp,))
            c.execute("INSERT INTO user_restrictions (telegram_user_id, created_at, expires_at, reason, created_by, admin_action_id) "
                      "VALUES (2, ?, '2026-10-02T00:00:00.000000+00:00', 'existing restriction', 999, 1)", (timestamp,))
            c.execute("INSERT INTO usage_requests (request_id, telegram_user_id, timestamp, model, provider, status, counts_toward_limit, accounted_tokens) "
                      "VALUES ('legacy-request', 2, ?, 'grok_4_6', 'amazon_bedrock', 'succeeded', 1, 7)", (timestamp,))
            c.execute("INSERT INTO usage_attempts (attempt_id, request_id, model, provider, timestamp, status, total_tokens) "
                      "VALUES ('legacy-attempt', 'legacy-request', 'grok_4_6', 'amazon_bedrock', ?, 'succeeded', 7)", (timestamp,))
            original = c.execute("SELECT * FROM users WHERE telegram_user_id = 2").fetchone()
            checksum = c.execute("SELECT checksum FROM schema_migrations WHERE version = 1").fetchone()[0]
            migrate(c, MIGRATIONS[:2])
            self.assertEqual(original, c.execute("SELECT * FROM users WHERE telegram_user_id = 2").fetchone())
            self.assertEqual(c.execute("SELECT selected_model FROM user_settings").fetchone()[0], "grok_4_6")
            self.assertEqual(c.execute("SELECT daily_request_limit FROM user_limits").fetchone()[0], 7)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM admin_audit_log").fetchone()[0], 1)
            for table in ("moderation_events", "user_warnings", "user_restrictions", "usage_requests", "usage_attempts"):
                self.assertEqual(c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 1)
            self.assertEqual(c.execute("SELECT accounted_tokens FROM usage_requests").fetchone()[0], 7)
            self.assertEqual(c.execute("SELECT total_tokens, telegram_user_id, counts_toward_limit FROM usage_attempts").fetchone(), (7, 2, 1))
            self.assertEqual(c.execute("SELECT tier, grok_allowed FROM user_access WHERE telegram_user_id = 2").fetchone(), ("STANDARD", 0))
            self.assertEqual(checksum, "51bdd36f844a233ccc3b9076c6c5fde1a5835435b7a94bdb4a351014b7352e11")
            self.assertEqual(c.execute("SELECT checksum FROM schema_migrations WHERE version = 1").fetchone()[0], checksum)
            self.assertEqual(c.execute("PRAGMA user_version").fetchone()[0], 2)
            self.assertEqual(c.execute("PRAGMA foreign_key_check").fetchall(), [])
