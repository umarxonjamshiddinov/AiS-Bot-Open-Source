import asyncio
import sqlite3
import threading
import time
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiogram.types import CallbackQuery, Message
from aiogram.types import Update
from aiogram.methods import EditMessageText, SendMessage

from Handlers.admin import admin_handler
from Handlers.auth import AuthMiddleware
from Storage.errors import StorageError
from Storage.maintenance import backup_database, verify_database
from Storage.migrations import MIGRATIONS, Migration, migrate
from Storage.models import AccessDeniedError, UserProfile, utc_timestamp
from Storage.sqlite import SQLiteStorage
from Tests.storage_support import StorageTestCase
from Tests.test_auth_middleware import _callback
from Tests.test_ui_english import telegram_message
from Tests.telegram_support import LocalTelegramSession
from config import PROJECT_ROOT, Settings, database_path


class PersistenceTests(StorageTestCase):
    async def test_profiles_update_identity_not_username_and_survive_restart(self):
        await self.add_user(2)
        first = await self.storage.users.get(2)
        await self.users.select_model((10, 2), "amazon_bedrock", "deepseek_v3_1")
        await self.users.select_model((11, 2), "amazon_bedrock", "gpt_oss_120b")
        await self.users.observe(SimpleNamespace(id=2, username="renamed", first_name="New", last_name="Name"))
        await self.add_user(3, username="renamed")
        await self.reopen()
        user = await self.storage.users.get(2)
        self.assertEqual(user.created_at, first.created_at)
        self.assertGreaterEqual(user.last_seen_at, first.last_seen_at)
        self.assertEqual((user.username, user.first_name, user.last_name), ("renamed", "New", "Name"))
        self.assertTrue((await self.users.access(2)).allowed)
        self.assertEqual((await self.users.selection((10, 2))).model_key, "deepseek_v3_1")
        self.assertEqual((await self.users.selection((11, 2))).model_key, "gpt_oss_120b")
        self.assertIsNone((await self.users.selection((10, 3))).model_key)
        self.assertEqual(len(await self.rows("SELECT * FROM users")), 2)
        await self.users.observe(SimpleNamespace(id=2, username=None, first_name="New", last_name=None))
        self.assertIsNone((await self.storage.users.get(2)).username)
        self.assertEqual(self.state.histories, {})
        self.assertFalse(hasattr(self.state, "authorized_users"))

    async def test_public_access_restart_ban_and_unban_in_middleware(self):
        middleware = AuthMiddleware(state=self.state, users=self.users)
        handler = AsyncMock(return_value="ok")
        with patch.object(Message, "answer", AsyncMock()):
            await middleware(handler, telegram_message("Hello"), {})
            handler.assert_awaited_once()
        await self.reopen()
        middleware = AuthMiddleware(state=self.state, users=self.users)
        self.assertEqual(await middleware(handler, telegram_message("Hello"), {}), "ok")
        handler.reset_mock()
        await self.admin.act(999, "block", user_id=2, reason="Policy violation")
        await self.reopen()
        middleware = AuthMiddleware(state=self.state, users=self.users)
        with patch.object(Message, "answer", AsyncMock()) as answer, patch.object(CallbackQuery, "answer", AsyncMock()):
            await middleware(handler, telegram_message("password"), {})
            await middleware(handler, _callback(2), {})
            self.assertIn("заблокирован", answer.call_args.args[0])
            handler.assert_not_awaited()
        self.assertEqual((await self.users.authorize(2)).code, "blocked")
        await self.admin.act(999, "unblock", user_id=2, reason="Resolved")
        self.assertTrue((await self.users.access(2)).allowed)
        audit = await self.admin.audit(999, 2)
        self.assertEqual([row["action"] for row in audit], ["unblock", "block"])

    async def test_preemptive_ban_is_not_removed_by_first_profile_update(self):
        await self.admin.act(999, "block", user_id=77, reason="Preemptive restriction")
        await self.add_user(77)
        user = await self.storage.users.get(77)
        self.assertTrue(user.is_blocked)
        self.assertEqual(user.status, "pending")
        self.assertEqual(user.blocked_by, 999)
        self.assertEqual(user.first_name, "Tester")

    async def test_restrictions_warnings_and_limits_persist_and_expire(self):
        await self.add_user()
        await self.admin.act(999, "restrict", user_id=2, reason="Cooldown", minutes=10)
        await self.admin.act(999, "warn", user_id=2, reason="Violation")
        await self.admin.act(999, "set_limit", user_id=2, reason="Quota", daily_request_limit=3)
        await self.reopen()
        self.assertEqual((await self.users.access(2)).code, "restricted")
        future = utc_timestamp(datetime.now(timezone.utc) + timedelta(minutes=11))
        self.assertTrue((await self.storage.users.access(2, future)).allowed)
        summary = await self.admin.user(999, 2)
        self.assertEqual(summary["active_warnings"], 1)
        self.assertEqual(summary["daily_request_limit"], 3)
        await self.admin.act(999, "unrestrict", user_id=2, reason="Reviewed")
        await self.admin.act(999, "clear_warnings", user_id=2, reason="Cleared")
        await self.admin.act(999, "set_limit", user_id=2, reason="Unlimited", daily_request_limit=None)
        self.assertTrue((await self.users.access(2)).allowed)
        summary = await self.admin.user(999, 2)
        self.assertEqual(summary["active_warnings"], 0)
        self.assertIsNone(summary["daily_request_limit"])
        self.assertEqual(len(await self.admin.audit(999, 2)), 6)

    async def test_quota_reservation_is_atomic_for_concurrent_requests(self):
        await self.add_user()
        await self.admin.act(999, "set_limit", user_id=2, reason="Quota", daily_request_limit=1)
        await asyncio.gather(*[
            self.storage.usage.start_request(request_id=str(n), user_id=2, timestamp=utc_timestamp(),
                                             model="gpt_oss_120b", provider="amazon_bedrock")
            for n in range(8)
        ])
        results = await asyncio.gather(*[
            self.storage.usage.start_attempt(attempt_id=str(n), request_id=str(n), user_id=2,
                                             timestamp=utc_timestamp(), model="gpt_oss_120b", provider="amazon_bedrock")
            for n in range(8)
        ])
        self.assertEqual(sum(result.allowed for result in results), 1)
        self.assertEqual(sum(result.code == "limit_exceeded" for result in results), 7)
        await self.reopen()
        result = await self.storage.usage.start_request(
            request_id="after-restart", user_id=2, timestamp=utc_timestamp(), model="m", provider="p")
        # Quota admission now precedes even the free technical moderation call.
        self.assertEqual(result.code, "limit_exceeded")
        self.assertEqual(len(await self.rows("SELECT * FROM usage_attempts")), 1)

    async def test_audit_and_target_change_rollback_together(self):
        await self.add_user()
        await self.storage.database.run(lambda connection: connection.execute(
            "CREATE TRIGGER reject_audit BEFORE INSERT ON admin_audit_log "
            "BEGIN SELECT RAISE(ABORT, 'audit unavailable'); END"), write=True)
        with self.assertRaises(StorageError):
            await self.admin.act(999, "block", user_id=2, reason="Must roll back")
        self.assertFalse((await self.storage.users.get(2)).is_blocked)
        self.assertEqual(await self.rows("SELECT * FROM admin_audit_log"), [])
        self.assertIsNone(await self.storage.users.get(999))

    async def test_admin_checks_and_moderation_links(self):
        await self.add_user()
        event_id = await self.moderation.record(
            user_id=2, request_id="request", model="m", provider="p", category="test_rule",
            severity="high", source="input", action="flag", code="test_rule", guardrail="test_guardrail")
        with self.assertRaises(PermissionError):
            await self.admin.act(2, "block", user_id=2, reason="No admin privilege")
        with self.assertRaises(PermissionError):
            await self.admin.events(2, 2)
        with self.assertRaises(ValueError):
            await self.admin.act(999, "block", user_id=3, event_id=event_id, reason="Wrong target")
        result = await self.admin.act(999, "warn", user_id=2, event_id=event_id, reason="Manual review")
        event = (await self.admin.events(999, 2))[0]
        self.assertEqual(event["admin_action_id"], result.audit_id)
        await self.admin.act(999, "review_moderation", event_id=event_id, review_status="resolved", reason="Handled")
        await self.reopen()
        self.assertEqual((await self.admin.events(999, 2))[0]["review_status"], "resolved")
        self.assertEqual(len(await self.admin.audit(999, 2)), 2)

    async def test_admin_interface_is_private_and_requires_configured_id(self):
        context = SimpleNamespace(admin=self.admin)
        answer = AsyncMock()
        await admin_handler(SimpleNamespace(from_user=SimpleNamespace(id=2), answer=answer), context)
        self.assertIn("Administrator", answer.call_args.args[0])
        await admin_handler(SimpleNamespace(from_user=SimpleNamespace(id=999), answer=answer,
                                           chat=SimpleNamespace(type="group")), context)
        self.assertIn("private", answer.call_args.args[0])
        await admin_handler(SimpleNamespace(from_user=SimpleNamespace(id=999), answer=answer,
                            chat=SimpleNamespace(type="private"), text="/admin block 2 test reason"), context)
        self.assertIn("Audit ID:", answer.call_args.args[0])
        self.assertTrue((await self.storage.users.get(2)).is_blocked)

    async def test_storage_outage_fails_closed(self):
        await self.add_user()
        await self.storage.close()
        middleware = AuthMiddleware(state=self.state, users=self.users)
        handler = AsyncMock()
        with patch.object(Message, "answer", AsyncMock()) as answer:
            await middleware(handler, telegram_message("Hello"), {})
            self.assertIn("недоступен", answer.call_args.args[0])
        handler.assert_not_awaited()

    async def test_sql_parameters_and_redacted_admin_reasons(self):
        payload = "'; DROP TABLE users; --"
        await self.add_user(username=payload)
        await self.admin.act(999, "warn", user_id=2,
                             reason="token=do-not-store test-secret-value " + payload)
        self.assertEqual((await self.storage.users.get(2)).username, payload)
        audit = (await self.admin.audit(999, 2))[0]
        self.assertNotIn("do-not-store", audit["reason"])
        self.assertNotIn("test-secret-value", audit["reason"])
        self.assertIn("[redacted]", audit["reason"])

    async def test_retention_preserves_current_bans_warnings_limits_and_settings(self):
        await self.add_user()
        await self.users.select_model((1, 2), "amazon_bedrock", "deepseek_v3_1")
        await self.admin.act(999, "block", user_id=2, reason="Block")
        await self.admin.act(999, "warn", user_id=2, reason="Warn")
        event = await self.moderation.record(
            user_id=2, request_id="r", model="m", provider="p", category="policy", severity="unknown",
            source="provider", action="block", code="content_filter", guardrail="upstream")
        await self.admin.act(999, "review_moderation", event_id=event, review_status="resolved", reason="Done")
        old = "2000-01-01T00:00:00.000000+00:00"
        for table in ("moderation_events", "admin_audit_log"):
            await self.storage.database.run(
                lambda connection: connection.execute(f"UPDATE {table} SET timestamp = ?", (old,)), write=True)
        cutoff = "2020-01-01T00:00:00.000000+00:00"
        counts = await self.storage.purge(usage_before=cutoff, moderation_before=cutoff, audit_before=cutoff, batch_size=1)
        self.assertEqual(counts["moderation_events"], 1)
        self.assertEqual(counts["admin_audit_log"], 3)
        self.assertTrue((await self.storage.users.get(2)).is_blocked)
        self.assertEqual((await self.admin.user(999, 2))["active_warnings"], 1)
        self.assertEqual((await self.users.selection((1, 2))).model_key, "deepseek_v3_1")
        self.assertIsNone((await self.rows("SELECT * FROM user_warnings"))[0]["admin_action_id"])


class DatabaseSafetyTests(StorageTestCase):
    async def test_runtime_lease_blocks_second_bot_but_allows_backup_readers(self):
        await self.storage.acquire_runtime()
        other = SQLiteStorage(self.path)
        try:
            await other.open()
            self.assertIsNone(await other.users.get(2))
            with self.assertRaisesRegex(RuntimeError, "Another bot"):
                await other.acquire_runtime()
            await self.storage.close()
            await other.acquire_runtime()
        finally:
            await other.close()

    async def test_main_build_initializes_storage_before_polling_and_closes_it(self):
        from main import build_application
        path = self.path.parent / "runtime.sqlite3"
        settings = Settings(bot_token="123456:fake-test-token",
                            ais_api_key="fake", ais_api_base_url="https://ais.example/v1",
                            tavily_api_key="fake", sqlite_path=path, admin_user_ids=frozenset({999}))
        bot, dispatcher, context = build_application(settings)
        try:
            self.assertFalse(path.exists())
            with patch.object(context.client, "validate_catalog", AsyncMock()):
                await context.start()
            self.assertTrue(path.is_file())
            self.assertIs(dispatcher["app_context"], context)
            self.assertIs(context.requests.router, context.ai_router)
            from AI.input_moderation import InputModerator
            self.assertIsInstance(context.moderation.input_moderator, InputModerator)
            self.assertIs(context.moderation.input_moderator.client, context.safety_client)
            self.assertIs(context.moderation.input_moderator.semaphore, context.ai_router._semaphore)
            from Services.safety_shadow import SafetyShadow
            self.assertIsInstance(context.moderation.shadow, SafetyShadow)
            self.assertIsNot(context.moderation.shadow.classifier.client, context.client)
            self.assertIsNot(context.moderation.shadow.classifier.semaphore, context.ai_router._semaphore)
            self.assertEqual((await context.storage.moderation.policy_state())["policy_version"], "ais_safety_v1")
            self.assertEqual(verify_database(path)["schema_version"], len(MIGRATIONS))
            await bot.session.close()
            session = LocalTelegramSession()
            bot.session = session

            async def feed_message(uid, text, update_id):
                payload = {"update_id": update_id, "message": {"message_id": update_id,
                    "date": int(datetime.now(timezone.utc).timestamp()), "chat": {"id": uid, "type": "private"},
                    "from": {"id": uid, "is_bot": False, "first_name": "Test"}, "text": text}}
                await dispatcher.feed_update(bot, Update.model_validate(payload, context={"bot": bot}))

            async def feed_callback(data, update_id):
                panel = session.sent[-1].model_dump(mode="json", exclude_none=True)
                payload = {"update_id": update_id, "callback_query": {"id": str(update_id), "chat_instance": "test",
                    "from": {"id": 999, "is_bot": False, "first_name": "Owner"}, "message": panel, "data": data}}
                await dispatcher.feed_update(bot, Update.model_validate(payload, context={"bot": bot}))

            await feed_message(2, "/start", 1)
            self.assertEqual((await context.accounts.status(2))["tier"], "STANDARD")
            await feed_message(2, "/admin", 2)
            self.assertIn("только для администратора", session.sent[-1].text)
            await feed_message(999, "/admin", 3)
            self.assertIn("Админ-панель", session.sent[-1].text)
            await feed_callback("adm:act:warn:2", 4)
            await feed_message(999, "Dispatcher integration reason", 5)
            fsm = dispatcher.fsm.get_context(bot=bot, chat_id=999, user_id=999)
            data = await fsm.get_data()
            self.assertIn("nonce", data)
            await asyncio.gather(feed_callback(f"adm:confirm:{data['nonce']}", 6), feed_callback(f"adm:confirm:{data['nonce']}", 7))
            self.assertEqual((await context.admin.user(999, 2))["active_warnings"], 1)
            self.assertEqual(len(await context.admin.audit(999, 2)), 1)
            self.assertTrue(any(isinstance(method, EditMessageText) for method in session.methods))
        finally:
            await context.close()
            await bot.session.close()

    async def test_pragmas_constraints_and_worker_do_not_block_event_loop(self):
        values = await self.storage.database.run(lambda connection: {
            name: connection.execute(f"PRAGMA {name}").fetchone()[0]
            for name in ("journal_mode", "foreign_keys", "busy_timeout", "synchronous", "user_version")})
        self.assertEqual(values, dict(journal_mode="wal", foreign_keys=1, busy_timeout=5000, synchronous=2, user_version=len(MIGRATIONS)))
        main_thread = threading.get_ident()
        self.assertNotEqual(await self.storage.database.run(lambda _: threading.get_ident()), main_thread)
        started = threading.Event()

        def slow_transaction(connection):
            started.set()
            time.sleep(0.12)
        job = asyncio.create_task(self.storage.database.run(slow_transaction))
        while not started.is_set():
            await asyncio.sleep(0.001)
        await asyncio.sleep(0.01)
        self.assertFalse(job.done())
        await job
        with self.assertRaises(StorageError):
            await self.storage.database.run(lambda connection: connection.execute(
                "INSERT INTO user_settings VALUES (123, 1, 'm', 'p', 'now')"), write=True)

    async def test_migration_upgrade_preserves_data_and_refuses_changed_ledger(self):
        await self.add_user()
        second = Migration(len(MIGRATIONS) + 1, "example_future_change", ("ALTER TABLE users ADD COLUMN test_note TEXT",))
        migrations = (*MIGRATIONS, second)
        await self.storage.database.run(lambda connection: migrate(connection, migrations))
        await self.storage.database.run(lambda connection: migrate(connection, migrations))
        self.assertEqual((await self.rows("SELECT status FROM users WHERE telegram_user_id = 2"))[0]["status"], "authorized")
        self.assertEqual(len(await self.rows("SELECT * FROM schema_migrations")), len(MIGRATIONS) + 1)
        with self.assertRaises(RuntimeError):
            await self.storage.database.run(lambda connection: migrate(connection))
        changed = (replace(MIGRATIONS[0], name="changed"), *MIGRATIONS[1:], second)
        with self.assertRaises(RuntimeError):
            await self.storage.database.run(lambda connection: migrate(connection, changed))

    async def test_failed_migration_rolls_back_schema_and_ledger(self):
        second = Migration(len(MIGRATIONS) + 1, "broken", ("CREATE TABLE should_not_exist (id INTEGER)", "INVALID SQL"))
        with self.assertRaises(StorageError):
            await self.storage.database.run(lambda connection: migrate(connection, (*MIGRATIONS, second)))
        self.assertEqual(len(await self.rows("SELECT * FROM schema_migrations")), len(MIGRATIONS))
        self.assertEqual(await self.rows("SELECT name FROM sqlite_master WHERE name = 'should_not_exist'"), [])

    async def test_online_wal_backup_restore_and_no_overwrite(self):
        await self.add_user()
        await self.users.select_model((1, 2), "amazon_bedrock", "deepseek_v3_1")
        await self.storage.usage.start_request(request_id="saved-request", user_id=2,
            timestamp=utc_timestamp(), model="deepseek_v3_1", provider="amazon_bedrock")
        await self.storage.usage.finish_request(request_id="saved-request", status="succeeded",
                                                timestamp=utc_timestamp(), latency_ms=2)
        event_id = await self.moderation.record(user_id=2, request_id="saved-request", model="deepseek_v3_1",
            provider="amazon_bedrock", category="test", severity="low", source="input", action="flag",
            code="test", guardrail="test")
        await self.admin.act(999, "warn", user_id=2, event_id=event_id, reason="Warning")
        await self.admin.act(999, "restrict", user_id=2, reason="Cooldown", minutes=5)
        await self.admin.act(999, "set_limit", user_id=2, reason="Quota", daily_request_limit=10)
        await self.admin.act(999, "block", user_id=2, reason="Persisted ban")
        backup = self.path.parent / "backup.sqlite3"
        result = await asyncio.to_thread(backup_database, self.path, backup)
        self.assertEqual(result["integrity"], "ok")
        self.assertTrue(Path(str(self.path) + "-wal").exists())
        await self.admin.act(999, "unblock", user_id=2, reason="After snapshot")
        restored = self.path.parent / "restored.sqlite3"
        await asyncio.to_thread(backup_database, backup, restored)
        self.assertEqual(verify_database(restored)["schema_version"], len(MIGRATIONS))
        with closing(sqlite3.connect(restored)) as connection:
            self.assertEqual(connection.execute("SELECT is_blocked FROM users WHERE telegram_user_id = 2").fetchone()[0], 1)
            for table in ("user_settings", "usage_requests", "moderation_events", "user_warnings", "user_restrictions", "user_limits"):
                self.assertEqual(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM admin_audit_log").fetchone()[0], 4)
        with self.assertRaises(FileExistsError):
            await asyncio.to_thread(backup_database, self.path, backup)
        self.assertFalse(Path(str(backup) + "-wal").exists())

    def test_config_path_is_relative_to_project_not_cwd(self):
        with patch.dict("os.environ", {"SQLITE_PATH": "data/custom.sqlite3"}):
            self.assertEqual(database_path(), (PROJECT_ROOT / "data/custom.sqlite3").resolve())
        with patch.dict("os.environ", {"SQLITE_PATH": ":memory:"}):
            with self.assertRaises(RuntimeError):
                database_path()
