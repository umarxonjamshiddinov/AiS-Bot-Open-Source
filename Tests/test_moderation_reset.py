import sqlite3
from contextlib import closing
from dataclasses import replace

from Storage.errors import StorageError
from Storage.maintenance import backup_database, reset_operational_state, verify_database
from Storage.migrations import MIGRATIONS, migrate
from Storage.models import utc_timestamp
from Storage.sqlite import SQLiteStorage
from Tests.moderation_support import ModerationTestCase
from config import PROJECT_ROOT


V1_CHECKSUM = "51bdd36f844a233ccc3b9076c6c5fde1a5835435b7a94bdb4a351014b7352e11"
V2_CHECKSUM = "81387aa90882a9326e0b74986761c1b0c9997e847fd1902914bdcf5228029289"
V3_CHECKSUM = "7859dbbca5d226a726454380db5b16214e89a2dfb9fe23361fcd3b9ef7cd41b9"
V4_CHECKSUM = "5b55d61011f900a87315dac9454fda41a64ec53b88bb578a3ac68b0a11832c90"


class ModerationMigrationTests(ModerationTestCase):
    def legacy_database(self):
        path = self.path.parent / "existing-v2.sqlite3"
        stamp = utc_timestamp()
        with closing(sqlite3.connect(path, isolation_level=None)) as c:
            c.execute("PRAGMA foreign_keys = ON")
            migrate(c, MIGRATIONS[:2])
            c.execute("INSERT INTO users (telegram_user_id, username, created_at, status) VALUES (999, 'owner', ?, 'authorized')", (stamp,))
            c.execute("INSERT INTO users (telegram_user_id, username, first_name, created_at, status, is_blocked, blocked_at, blocked_by, block_reason) "
                      "VALUES (2, 'old-user', 'Existing', ?, 'authorized', 1, ?, 999, 'Legacy unclassified ban')", (stamp, stamp))
            c.execute("INSERT INTO user_access (telegram_user_id) VALUES (999)")
            c.execute("INSERT INTO user_access (telegram_user_id, tier, trusted_source, trusted_at, trusted_by, grok_allowed, grok_source, grok_granted_at) "
                      "VALUES (2, 'TRUSTED', 'manual_admin', ?, 999, 1, 'access_code', ?)", (stamp, stamp))
            c.execute("INSERT INTO user_settings VALUES (2, 2, 'grok_4_6', 'amazon_bedrock', ?)", (stamp,))
            c.execute("INSERT INTO admin_audit_log (id, timestamp, admin_id, action, target_user_id, resource_type, reason) "
                      "VALUES (1, ?, 999, 'warn', 2, 'user', 'Existing manual warning')", (stamp,))
            c.execute("INSERT INTO moderation_events (id, timestamp, telegram_user_id, category, severity, source, action, code, guardrail) "
                      "VALUES (7, ?, 2, 'unknown', 'unknown', 'provider', 'block', 'content_filter', 'upstream')", (stamp,))
            c.execute("INSERT INTO user_warnings (id, telegram_user_id, created_at, reason, created_by, admin_action_id, moderation_event_id) "
                      "VALUES (42, 2, ?, 'Existing warning', 999, 1, 7)", (stamp,))
            c.execute("UPDATE sqlite_sequence SET seq = 200 WHERE name = 'user_warnings'")
            c.execute("INSERT INTO trusted_applications (id, telegram_user_id, created_at) VALUES (1, 2, ?)", (stamp,))
            c.execute("INSERT INTO notification_outbox (id, recipient_id, application_id, kind, created_at, next_attempt_at) "
                      "VALUES (7, 999, 1, 'trusted_pending', ?, ?)", (stamp, stamp))
            c.execute("UPDATE sqlite_sequence SET seq = 99 WHERE name = 'notification_outbox'")
            c.execute("INSERT INTO activation_attempts VALUES (2, 'premium', ?, ?, 2, NULL)", (stamp, stamp))
            c.execute("INSERT INTO usage_requests (request_id, telegram_user_id, timestamp, model, provider, status, counts_toward_limit) "
                      "VALUES ('old-request', 2, ?, 'grok_4_6', 'amazon_bedrock', 'succeeded', 0)", (stamp,))
            c.execute("INSERT INTO usage_attempts (attempt_id, request_id, model, provider, timestamp, status, total_tokens, telegram_user_id) "
                      "VALUES ('old-attempt', 'old-request', 'grok_4_6', 'amazon_bedrock', ?, 'succeeded', 7, 2)", (stamp,))
        return path

    def test_v1_and_v2_sql_checksums_are_unchanged(self):
        self.assertEqual(MIGRATIONS[0].checksum, V1_CHECKSUM)
        self.assertEqual(MIGRATIONS[1].checksum, V2_CHECKSUM)
        self.assertEqual(MIGRATIONS[0].name, "persistent_application_state")
        self.assertEqual(MIGRATIONS[1].name, "public_access_trusted_grok_and_inference_quotas")
        self.assertEqual(MIGRATIONS[2].version, 3)
        self.assertEqual(MIGRATIONS[2].checksum, V3_CHECKSUM)
        self.assertEqual(MIGRATIONS[3].checksum, V4_CHECKSUM)

    async def test_v3_upgrades_existing_db_with_verified_backup_and_preserved_data(self):
        path = self.legacy_database()
        self.assertEqual(verify_database(path)["schema_version"], 2)
        upgraded = SQLiteStorage(path)
        try:
            await upgraded.open()
            self.assertEqual(verify_database(path)["integrity"], "ok")
            self.assertEqual(verify_database(path)["schema_version"], len(MIGRATIONS))
            account = await upgraded.accounts.snapshot(2, utc_timestamp())
            self.assertEqual((account["tier"], account["grok_allowed"], account["requests_today"]), ("TRUSTED", 1, 1))
            user = await upgraded.users.get(2)
            self.assertEqual((user.username, user.first_name, user.is_blocked, user.block_reason),
                             ("old-user", "Existing", 1, "Legacy unclassified ban"))
            self.assertEqual(user.block_source, "legacy_unknown")
            self.assertEqual((await upgraded.users.selection(2, 2)).model_key, "grok_4_6")
            self.assertEqual(await upgraded.moderation.active_warnings(2), 0)
            rows = await upgraded.database.run(lambda c: dict(c.execute("SELECT * FROM user_warnings WHERE id = 42").fetchone()))
            self.assertEqual((rows["source"], rows["status"], rows["moderation_event_id"]), ("manual_admin", "active", 7))
            sequences = await upgraded.database.run(lambda c: dict(c.execute(
                "SELECT name, seq FROM sqlite_sequence WHERE name IN ('user_warnings', 'notification_outbox')")))
            self.assertEqual(sequences, {"user_warnings": 200, "notification_outbox": 99})
            notices = await upgraded.notifications.pending(utc_timestamp())
            self.assertEqual((notices[0]["id"], notices[0]["kind"]), (7, "trusted_pending"))
            self.assertEqual(await upgraded.database.run(lambda c: c.execute("SELECT failure_count FROM activation_attempts").fetchone()[0]), 2)
            ledger = await upgraded.database.run(lambda c: [row[0] for row in c.execute("SELECT checksum FROM schema_migrations WHERE version <= 2 ORDER BY version")])
            self.assertEqual(ledger, [V1_CHECKSUM, V2_CHECKSUM])
        finally:
            await upgraded.close()
        backups = list((path.parent / "backups").glob("*.before-v3-*.sqlite3"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(verify_database(backups[0])["schema_version"], 2)
        self.assertEqual(verify_database(backups[0])["integrity"], "ok")

    def test_failed_v3_rolls_back_original_schema_and_data(self):
        path = self.legacy_database()
        broken = replace(MIGRATIONS[2], statements=(*MIGRATIONS[2].statements, "INVALID SQL"))
        with closing(sqlite3.connect(path, isolation_level=None)) as c:
            c.execute("PRAGMA foreign_keys = ON")
            with self.assertRaises(sqlite3.Error):
                migrate(c, (*MIGRATIONS[:2], broken))
            self.assertEqual(c.execute("PRAGMA user_version").fetchone()[0], 2)
            self.assertEqual(c.execute("SELECT reason FROM user_warnings WHERE id = 42").fetchone()[0], "Existing warning")
            self.assertIsNone(c.execute("SELECT name FROM sqlite_master WHERE name = 'moderation_appeals'").fetchone())
        self.assertEqual(verify_database(path)["integrity"], "ok")


class ModerationResetTests(ModerationTestCase):
    async def seed_operational_data(self):
        await self.add_user(3, username="trusted-user")
        application = await self.accounts.apply_trusted(3)
        await self.admin.act(999, "approve_trusted", application_id=application["id"], reason="Keep Trusted approval")
        await self.accounts.activate(3, "grok", self.settings.grok_access_code)
        await self.users.select_model((3, 3), "amazon_bedrock", "grok_4_6")
        await self.accounts.activate(2, "premium", "incorrect-test-code")
        for index, uid in enumerate((2, 3, 2)):
            stamp = "2000-01-01T00:00:00.000000+00:00" if index == 2 else utc_timestamp()
            request = f"reset-request-{index}"
            await self.storage.usage.start_request(request_id=request, user_id=uid, timestamp=stamp, model="gpt_oss_120b", provider="amazon_bedrock")
            await self.storage.usage.start_attempt(attempt_id=request, request_id=request, user_id=uid, timestamp=stamp, model="gpt_oss_120b", provider="amazon_bedrock")
        await self.ban(3)
        await self.submit_appeal(3)
        await self.admin.act(999, "warn", user_id=2, reason="Manual warning to reset")
        await self.admin.act(999, "block", user_id=2, reason="Manual ban to preserve")
        await self.admin.act(999, "restrict", user_id=2, reason="Manual restriction to preserve", minutes=20)
        await self.add_user(77)
        await self.admin.act(999, "block", user_id=77, reason="Legacy ban to preserve")
        await self.storage.database.run(lambda c: c.execute("UPDATE users SET block_source = 'legacy_unknown' WHERE telegram_user_id = 77"), write=True)
        await self.storage.database.run(lambda c: c.execute(
            "INSERT INTO user_restrictions (telegram_user_id, created_at, expires_at, reason, created_by, source) "
            "VALUES (3, '2000-01-01', '2000-01-02', 'Old structured automatic restriction', 999, 'automatic_moderation')"), write=True)

    async def protected(self):
        return {table: await self.rows(f"SELECT * FROM {table} ORDER BY 1") for table in (
            "users", "user_settings", "user_access", "trusted_applications", "user_limits", "admin_audit_log", "schema_migrations")}

    async def test_reset_clears_operations_preserves_accounts_and_restores_50_500_zero_warnings(self):
        await self.seed_operational_data()
        before = await self.protected()
        trusted_notices = await self.rows("SELECT * FROM notification_outbox WHERE application_id IS NOT NULL")
        backup = self.path.parent / "before-reset.sqlite3"
        counts = await reset_operational_state(self.path, backup)
        self.assertEqual(counts, {"notification_outbox": 1, "moderation_appeals": 1, "moderation_sanctions": 1,
                                 "user_restrictions": 1, "user_warnings": 4, "moderation_events": 3,
                                 "usage_attempts": 3, "usage_requests": 3, "activation_attempts": 1})
        self.assertEqual(before, await self.protected())
        self.assertEqual(trusted_notices, await self.rows("SELECT * FROM notification_outbox WHERE application_id IS NOT NULL"))
        await self.reopen()
        standard, trusted = await self.accounts.status(2), await self.accounts.status(3)
        self.assertEqual((standard["effective_daily_limit"], standard["requests_today"]), (50, 0))
        self.assertEqual((trusted["effective_daily_limit"], trusted["requests_today"]), (500, 0))
        self.assertEqual((trusted["tier"], trusted["trusted_source"], trusted["grok_allowed"]), ("TRUSTED", "admin_approval", 1))
        self.assertEqual(await self.storage.moderation.active_warnings(2), 0)
        self.assertEqual(await self.storage.moderation.active_warnings(3), 0)
        self.assertEqual((await self.users.selection((3, 3))).model_key, "grok_4_6")
        self.assertTrue(self.admin.is_admin(999))
        self.assertTrue((await self.users.access(3)).allowed)
        self.assertEqual((await self.users.access(2)).code, "blocked")  # Manual ban survives.
        self.assertEqual((await self.users.access(77)).code, "blocked")  # Unclassified ban survives.
        self.assertEqual(len(await self.rows("SELECT * FROM user_restrictions")), 1)
        self.assertEqual(verify_database(self.path)["integrity"], "ok")
        self.assertEqual(verify_database(backup)["integrity"], "ok")
        with closing(sqlite3.connect(backup)) as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM user_warnings").fetchone()[0], 4)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM moderation_appeals").fetchone()[0], 1)

    async def test_reset_requires_successful_backup_and_never_overwrites_it(self):
        await self.seed_operational_data()
        backup = self.path.parent / "existing-backup.sqlite3"
        backup_database(self.path, backup)
        with self.assertRaises(FileExistsError):
            await reset_operational_state(self.path, backup)
        self.assertEqual(await self.storage.moderation.active_warnings(3), 3)
        self.assertEqual(len(await self.rows("SELECT * FROM usage_requests")), 3)

    async def test_reset_refuses_running_bot_runtime_lease(self):
        await self.seed_operational_data()
        await self.storage.acquire_runtime()
        backup = self.path.parent / "must-not-exist.sqlite3"
        with self.assertRaises(RuntimeError):
            await reset_operational_state(self.path, backup)
        self.assertFalse(backup.exists())
        self.assertEqual(await self.storage.moderation.active_warnings(3), 3)

    async def test_reset_failure_rolls_back_all_deletions(self):
        await self.seed_operational_data()
        before = await self.protected()
        await self.storage.database.run(lambda c: c.execute(
            "CREATE TRIGGER reset_failure BEFORE DELETE ON usage_requests BEGIN SELECT RAISE(ABORT, 'test'); END"), write=True)
        with self.assertRaises(StorageError):
            await reset_operational_state(self.path, self.path.parent / "rollback-backup.sqlite3")
        self.assertEqual(before, await self.protected())
        self.assertEqual(await self.storage.moderation.active_warnings(3), 3)
        self.assertEqual(len(await self.rows("SELECT * FROM moderation_appeals")), 1)
        self.assertEqual(len(await self.rows("SELECT * FROM usage_attempts")), 3)
        self.assertEqual(verify_database(self.path)["integrity"], "ok")

    async def test_reset_cannot_target_another_project(self):
        with self.assertRaises(ValueError):
            await reset_operational_state(PROJECT_ROOT.parent / "outside.sqlite3", self.path.parent / "backup.sqlite3")
