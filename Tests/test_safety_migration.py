import sqlite3
from contextlib import closing
from dataclasses import replace

from Storage.maintenance import verify_database
from Storage.migrations import MIGRATIONS, migrate
from Storage.sqlite import SQLiteStorage
from Storage.models import utc_timestamp
from Tests.safety_support import AdaptiveTestCase, assessment_response, harmful
from Tests import test_moderation_reset as old_fixtures


class SafetyMigrationTests(AdaptiveTestCase):
    legacy_database = old_fixtures.ModerationMigrationTests.legacy_database

    async def test_v5_upgrade_preserves_v4_data_and_starts_with_v4_production(self):
        path = self.legacy_database()
        with closing(sqlite3.connect(path, isolation_level=None)) as c:
            c.execute("PRAGMA foreign_keys = ON")
            migrate(c, MIGRATIONS[:4])
            c.execute("UPDATE usage_requests SET input_moderation_status = 'allowed', input_moderation_policy = 'ais_safety_v1'")
            before = {}
            for table in ("users", "user_access", "user_settings", "user_warnings", "moderation_events", "usage_requests", "usage_attempts", "trusted_applications", "notification_outbox", "admin_audit_log", "schema_migrations"):
                columns = [row[1] for row in c.execute(f"PRAGMA table_info({table})")]
                before[table] = (columns, c.execute(f"SELECT {','.join(columns)} FROM {table} ORDER BY 1").fetchall())
        storage = SQLiteStorage(path)
        try:
            await storage.open()
            for table, (columns, expected) in before.items():
                condition = " WHERE version <= 4" if table == "schema_migrations" else ""
                actual = await storage.database.run(lambda c: [tuple(r) for r in c.execute(f"SELECT {','.join(columns)} FROM {table}{condition} ORDER BY 1")])
                self.assertEqual(expected, actual, table)
            policy = await storage.moderation.policy_state()
            self.assertEqual((policy["policy_version"], policy["revision"], policy["admin_action_id"]), ("ais_safety_v1", 0, None))
            self.assertEqual(await storage.database.run(lambda c: c.execute("SELECT COUNT(*) FROM safety_evaluations").fetchone()[0]), 0)
        finally:
            await storage.close()
        self.assertEqual(verify_database(path)["schema_version"], len(MIGRATIONS))
        self.assertEqual(verify_database(path)["integrity"], "ok")
        snapshot = next((path.parent / "backups").glob("*.before-v5-*.sqlite3"))
        self.assertEqual(verify_database(snapshot)["schema_version"], 4)

    def test_failed_v5_rolls_back_all_schema_and_policy_state(self):
        path = self.legacy_database()
        with closing(sqlite3.connect(path, isolation_level=None)) as c:
            c.execute("PRAGMA foreign_keys = ON")
            migrate(c, MIGRATIONS[:4])
            broken = replace(MIGRATIONS[4], statements=(*MIGRATIONS[4].statements, "INVALID SQL"))
            with self.assertRaises(sqlite3.Error):
                migrate(c, (*MIGRATIONS[:4], broken))
            self.assertEqual(c.execute("PRAGMA user_version").fetchone()[0], 4)
            self.assertIsNone(c.execute("SELECT name FROM sqlite_master WHERE name = 'safety_policy_state'").fetchone())
        self.assertEqual(verify_database(path)["integrity"], "ok")

    async def test_retention_preserves_v5_warning_appeal_evidence_and_policy_audit(self):
        service, _ = self.adaptive_service([assessment_response(harmful())] * 3)
        await self.activate()
        from AI.moderation import ProviderModerationError
        for _ in range(3):
            with self.assertRaises(ProviderModerationError):
                await service.ask_text(**self.request())
        appeal = await self.submit_appeal()
        await self.admin.act(999, "approve_appeal", appeal_id=appeal["id"], reason="Keep corrected decision history")
        for table, field in (("moderation_events", "timestamp"), ("safety_evaluations", "created_at"), ("admin_audit_log", "timestamp")):
            await self.storage.database.run(lambda c: c.execute(f"UPDATE {table} SET {field} = '2000-01-01T00:00:00.000000+00:00'"), write=True)
        await self.storage.purge(usage_before="2020-01-01", moderation_before="2020-01-01", audit_before="2020-01-01")
        remaining = await self.rows("SELECT mode FROM safety_evaluations")
        self.assertEqual(remaining, [{"mode": "production"}] * 3)
        self.assertEqual(len(await self.rows("SELECT * FROM moderation_events")), 3)
        self.assertEqual((await self.storage.moderation.policy_state())["policy_version"], "ais_safety_v2")
        self.assertEqual(len(await self.rows("SELECT * FROM admin_audit_log WHERE action IN ('approve_appeal', 'activate_safety_v5')")), 2)
