"""Append migrations; never modify an already deployed migration.

Every statement and its ledger entry run in a single explicit transaction.
Do not use executescript(): it can implicitly commit a migration halfway through.
"""
from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass

from Storage.models import utc_timestamp


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    statements: tuple[str, ...]

    @property
    def checksum(self) -> str:
        return hashlib.sha256("\n".join(self.statements).encode()).hexdigest()


MIGRATIONS = (Migration(1, "persistent_application_state", (
    """CREATE TABLE users (
        telegram_user_id INTEGER PRIMARY KEY CHECK (telegram_user_id > 0),
        username TEXT, first_name TEXT, last_name TEXT,
        created_at TEXT NOT NULL, last_seen_at TEXT,
        status TEXT NOT NULL DEFAULT 'pending'
            CHECK (status IN ('pending', 'authorized', 'revoked')),
        authorized_at TEXT,
        is_blocked INTEGER NOT NULL DEFAULT 0 CHECK (is_blocked IN (0, 1)),
        blocked_at TEXT, block_reason TEXT,
        blocked_by INTEGER REFERENCES users(telegram_user_id),
        CHECK ((is_blocked = 0 AND blocked_at IS NULL AND block_reason IS NULL
                AND blocked_by IS NULL) OR
               (is_blocked = 1 AND blocked_at IS NOT NULL AND block_reason IS NOT NULL
                AND blocked_by IS NOT NULL))
    )""",
    "CREATE INDEX ix_users_status ON users(status, is_blocked)",
    "CREATE INDEX ix_users_last_seen ON users(last_seen_at)",
    """CREATE TABLE user_settings (
        telegram_user_id INTEGER NOT NULL REFERENCES users(telegram_user_id),
        chat_id INTEGER NOT NULL,
        selected_model TEXT NOT NULL, selected_provider TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (telegram_user_id, chat_id)
    )""",
    """CREATE TABLE admin_audit_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT NOT NULL,
        admin_id INTEGER NOT NULL REFERENCES users(telegram_user_id),
        action TEXT NOT NULL,
        target_user_id INTEGER REFERENCES users(telegram_user_id),
        resource_type TEXT NOT NULL, resource_id TEXT,
        reason TEXT NOT NULL CHECK (length(reason) BETWEEN 1 AND 512),
        metadata_json TEXT NOT NULL DEFAULT '{}'
    )""",
    "CREATE INDEX ix_audit_user_time ON admin_audit_log(target_user_id, timestamp)",
    "CREATE INDEX ix_audit_admin_time ON admin_audit_log(admin_id, timestamp)",
    "CREATE INDEX ix_audit_time ON admin_audit_log(timestamp)",
    """CREATE TABLE moderation_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT NOT NULL,
        telegram_user_id INTEGER NOT NULL REFERENCES users(telegram_user_id),
        request_id TEXT, model TEXT, provider TEXT,
        category TEXT NOT NULL, severity TEXT NOT NULL
            CHECK (severity IN ('unknown', 'info', 'low', 'medium', 'high', 'critical')),
        source TEXT NOT NULL CHECK (source IN ('input', 'output', 'provider')),
        action TEXT NOT NULL CHECK (action IN ('allow', 'flag', 'block')),
        code TEXT NOT NULL, guardrail TEXT NOT NULL,
        admin_action_id INTEGER REFERENCES admin_audit_log(id) ON DELETE SET NULL,
        review_status TEXT NOT NULL DEFAULT 'open'
            CHECK (review_status IN ('open', 'resolved', 'dismissed')),
        reviewed_at TEXT
    )""",
    "CREATE INDEX ix_moderation_user_time ON moderation_events(telegram_user_id, timestamp)",
    "CREATE INDEX ix_moderation_request ON moderation_events(request_id)",
    "CREATE INDEX ix_moderation_review_time ON moderation_events(review_status, timestamp)",
    "CREATE INDEX ix_moderation_time ON moderation_events(timestamp)",
    "CREATE INDEX ix_moderation_audit ON moderation_events(admin_action_id)",
    """CREATE TABLE user_restrictions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        telegram_user_id INTEGER NOT NULL REFERENCES users(telegram_user_id),
        created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
        reason TEXT NOT NULL, created_by INTEGER NOT NULL REFERENCES users(telegram_user_id),
        admin_action_id INTEGER REFERENCES admin_audit_log(id) ON DELETE SET NULL,
        moderation_event_id INTEGER REFERENCES moderation_events(id) ON DELETE SET NULL,
        revoked_at TEXT, revoked_by INTEGER REFERENCES users(telegram_user_id),
        CHECK (expires_at > created_at),
        CHECK ((revoked_at IS NULL) = (revoked_by IS NULL))
    )""",
    "CREATE INDEX ix_restrictions_active ON user_restrictions(telegram_user_id, expires_at) WHERE revoked_at IS NULL",
    "CREATE INDEX ix_restrictions_audit ON user_restrictions(admin_action_id)",
    "CREATE INDEX ix_restrictions_event ON user_restrictions(moderation_event_id)",
    """CREATE TABLE user_warnings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        telegram_user_id INTEGER NOT NULL REFERENCES users(telegram_user_id),
        created_at TEXT NOT NULL, reason TEXT NOT NULL,
        created_by INTEGER NOT NULL REFERENCES users(telegram_user_id),
        admin_action_id INTEGER REFERENCES admin_audit_log(id) ON DELETE SET NULL,
        moderation_event_id INTEGER REFERENCES moderation_events(id) ON DELETE SET NULL,
        cleared_at TEXT, cleared_by INTEGER REFERENCES users(telegram_user_id),
        CHECK ((cleared_at IS NULL) = (cleared_by IS NULL))
    )""",
    "CREATE INDEX ix_warnings_active ON user_warnings(telegram_user_id) WHERE cleared_at IS NULL",
    "CREATE INDEX ix_warnings_audit ON user_warnings(admin_action_id)",
    "CREATE INDEX ix_warnings_event ON user_warnings(moderation_event_id)",
    """CREATE TABLE user_limits (
        telegram_user_id INTEGER PRIMARY KEY REFERENCES users(telegram_user_id),
        daily_request_limit INTEGER CHECK (daily_request_limit > 0),
        updated_at TEXT NOT NULL,
        updated_by INTEGER NOT NULL REFERENCES users(telegram_user_id)
    )""",
    """CREATE TABLE usage_requests (
        request_id TEXT PRIMARY KEY NOT NULL,
        telegram_user_id INTEGER NOT NULL REFERENCES users(telegram_user_id),
        timestamp TEXT NOT NULL, finished_at TEXT,
        model TEXT NOT NULL, provider TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN
            ('pending', 'succeeded', 'error', 'cancelled', 'interrupted', 'stale',
             'blocked', 'restricted', 'unauthorized', 'limit_exceeded')),
        counts_toward_limit INTEGER NOT NULL CHECK (counts_toward_limit IN (0, 1)),
        latency_ms INTEGER CHECK (latency_ms >= 0),
        input_tokens INTEGER CHECK (input_tokens >= 0),
        output_tokens INTEGER CHECK (output_tokens >= 0),
        total_tokens INTEGER CHECK (total_tokens >= 0),
        accounted_tokens INTEGER CHECK (accounted_tokens >= 0),
        usage_complete INTEGER NOT NULL DEFAULT 0 CHECK (usage_complete IN (0, 1)),
        error_type TEXT, error_code TEXT
    )""",
    "CREATE INDEX ix_usage_user_time ON usage_requests(telegram_user_id, timestamp)",
    "CREATE INDEX ix_usage_time ON usage_requests(timestamp)",
    "CREATE INDEX ix_usage_status ON usage_requests(status)",
    """CREATE TABLE usage_attempts (
        attempt_id TEXT PRIMARY KEY NOT NULL,
        request_id TEXT NOT NULL REFERENCES usage_requests(request_id) ON DELETE CASCADE,
        model TEXT NOT NULL, provider TEXT NOT NULL, timestamp TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN
            ('pending', 'succeeded', 'error', 'blocked', 'cancelled', 'interrupted')),
        latency_ms INTEGER CHECK (latency_ms >= 0),
        input_tokens INTEGER CHECK (input_tokens >= 0),
        output_tokens INTEGER CHECK (output_tokens >= 0),
        total_tokens INTEGER CHECK (total_tokens >= 0),
        error_type TEXT, error_code TEXT
    )""",
    "CREATE INDEX ix_attempts_request ON usage_attempts(request_id)",
    "CREATE INDEX ix_attempts_model_time ON usage_attempts(provider, model, timestamp)",
)),)


MIGRATIONS += (Migration(2, "public_access_trusted_grok_and_inference_quotas", (
    """CREATE TABLE user_access (
        telegram_user_id INTEGER PRIMARY KEY REFERENCES users(telegram_user_id),
        tier TEXT NOT NULL DEFAULT 'STANDARD' CHECK (tier IN ('STANDARD', 'TRUSTED')),
        trusted_source TEXT CHECK (trusted_source IN ('admin_approval', 'premium_code', 'manual_admin')),
        trusted_at TEXT, trusted_by INTEGER REFERENCES users(telegram_user_id),
        grok_allowed INTEGER NOT NULL DEFAULT 0 CHECK (grok_allowed IN (0, 1)),
        grok_source TEXT CHECK (grok_source IN ('access_code', 'manual_admin')),
        grok_granted_at TEXT, grok_granted_by INTEGER REFERENCES users(telegram_user_id),
        CHECK ((tier = 'STANDARD' AND trusted_source IS NULL AND trusted_at IS NULL AND trusted_by IS NULL)
            OR (tier = 'TRUSTED' AND trusted_source IS NOT NULL AND trusted_at IS NOT NULL)),
        CHECK ((grok_allowed = 0 AND grok_source IS NULL AND grok_granted_at IS NULL AND grok_granted_by IS NULL)
            OR (grok_allowed = 1 AND grok_source IS NOT NULL AND grok_granted_at IS NOT NULL))
    )""",
    "INSERT INTO user_access (telegram_user_id) SELECT telegram_user_id FROM users",
    "CREATE INDEX ix_access_tier ON user_access(tier, telegram_user_id)",
    """UPDATE users SET status = 'authorized', authorized_at = COALESCE(authorized_at, created_at)
        WHERE status = 'pending' AND is_blocked = 0""",
    """CREATE TABLE trusted_applications (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        telegram_user_id INTEGER NOT NULL REFERENCES users(telegram_user_id),
        status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected')),
        created_at TEXT NOT NULL, decided_at TEXT,
        decided_by INTEGER REFERENCES users(telegram_user_id), reason TEXT,
        admin_action_id INTEGER REFERENCES admin_audit_log(id) ON DELETE SET NULL
    )""",
    "CREATE UNIQUE INDEX ux_trusted_pending_user ON trusted_applications(telegram_user_id) WHERE status = 'pending'",
    "CREATE INDEX ix_trusted_status_time ON trusted_applications(status, created_at)",
    "CREATE INDEX ix_trusted_audit ON trusted_applications(admin_action_id)",
    """CREATE TABLE activation_attempts (
        telegram_user_id INTEGER NOT NULL REFERENCES users(telegram_user_id),
        purpose TEXT NOT NULL CHECK (purpose IN ('grok', 'premium')),
        window_started_at TEXT NOT NULL, last_attempt_at TEXT NOT NULL,
        failure_count INTEGER NOT NULL DEFAULT 0 CHECK (failure_count >= 0),
        locked_until TEXT, PRIMARY KEY (telegram_user_id, purpose)
    )""",
    """CREATE TABLE notification_outbox (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        recipient_id INTEGER NOT NULL,
        application_id INTEGER NOT NULL REFERENCES trusted_applications(id),
        kind TEXT NOT NULL CHECK (kind IN ('trusted_pending', 'trusted_approved', 'trusted_rejected')),
        created_at TEXT NOT NULL, sent_at TEXT,
        status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'sent', 'failed')),
        attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at TEXT NOT NULL,
        error_code TEXT,
        UNIQUE (recipient_id, application_id, kind)
    )""",
    "CREATE INDEX ix_notifications_pending ON notification_outbox(status, next_attempt_at)",
    "ALTER TABLE usage_requests ADD COLUMN requires_grok INTEGER NOT NULL DEFAULT 0 CHECK (requires_grok IN (0, 1))",
    "ALTER TABLE usage_attempts ADD COLUMN telegram_user_id INTEGER REFERENCES users(telegram_user_id)",
    "ALTER TABLE usage_attempts ADD COLUMN counts_toward_limit INTEGER NOT NULL DEFAULT 1 CHECK (counts_toward_limit IN (0, 1))",
    """UPDATE usage_attempts SET telegram_user_id =
        (SELECT telegram_user_id FROM usage_requests WHERE usage_requests.request_id = usage_attempts.request_id)""",
    "CREATE INDEX ix_attempts_user_time ON usage_attempts(telegram_user_id, timestamp, counts_toward_limit)",
)),)


from Storage.migration_v3 import STATEMENTS as MODERATION_V3

MIGRATIONS += (Migration(3, "automatic_moderation_warnings_bans_and_appeals", MODERATION_V3),)

from Storage.migration_v4 import STATEMENTS as INPUT_MODERATION_V4

MIGRATIONS += (Migration(4, "centralized_input_moderation_and_technical_usage", INPUT_MODERATION_V4),)

from Storage.migration_v5 import STATEMENTS as ADAPTIVE_SAFETY_V5

MIGRATIONS += (Migration(5, "adaptive_safety_shadow_review_and_policy_rollout", ADAPTIVE_SAFETY_V5),)

from Storage.migration_v6 import STATEMENTS as MODEL_ACCESS_V6

MIGRATIONS += (Migration(6, "premium_model_access_and_shared_higher_cost_quota", MODEL_ACCESS_V6),)


def migrate(connection: sqlite3.Connection, migrations=MIGRATIONS) -> None:
    versions = [migration.version for migration in migrations]
    if versions != list(range(1, len(versions) + 1)):
        raise RuntimeError("Migrations must have consecutive versions starting at 1")
    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute("""CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY, name TEXT NOT NULL,
            checksum TEXT NOT NULL, applied_at TEXT NOT NULL
        )""")
        applied = connection.execute(
            "SELECT version, name, checksum FROM schema_migrations ORDER BY version"
        ).fetchall()
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version != len(applied) or version > len(migrations):
            raise RuntimeError("Database schema is newer than or inconsistent with this application")
        for row, migration in zip(applied, migrations):
            if tuple(row) != (migration.version, migration.name, migration.checksum):
                raise RuntimeError("Applied migration was changed or ledger is inconsistent")
        for migration in migrations[version:]:
            for statement in migration.statements:
                connection.execute(statement)
            connection.execute(
                "INSERT INTO schema_migrations VALUES (?, ?, ?, ?)",
                (migration.version, migration.name, migration.checksum, utc_timestamp()),
            )
            connection.execute(f"PRAGMA user_version = {migration.version:d}")
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
