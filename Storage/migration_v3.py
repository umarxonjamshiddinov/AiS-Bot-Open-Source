"""Append-only moderation upgrade. The v1/v2 statements remain unchanged."""

STATEMENTS = (
    # v1 required an administrator as warning creator. Automatic actions have no
    # human actor; retain all old rows while making that distinction explicit.
    """CREATE TABLE user_warnings_v3 (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        telegram_user_id INTEGER NOT NULL REFERENCES users(telegram_user_id),
        created_at TEXT NOT NULL, reason TEXT NOT NULL,
        created_by INTEGER REFERENCES users(telegram_user_id),
        admin_action_id INTEGER REFERENCES admin_audit_log(id) ON DELETE SET NULL,
        moderation_event_id INTEGER REFERENCES moderation_events(id) ON DELETE SET NULL,
        cleared_at TEXT, cleared_by INTEGER REFERENCES users(telegram_user_id),
        source TEXT NOT NULL DEFAULT 'legacy_unknown'
            CHECK (source IN ('legacy_unknown', 'manual_admin', 'automatic_moderation')),
        status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'revoked', 'overturned')),
        CHECK ((status = 'active' AND cleared_at IS NULL AND cleared_by IS NULL)
            OR (status != 'active' AND cleared_at IS NOT NULL)),
        CHECK (source != 'automatic_moderation' OR (created_by IS NULL AND moderation_event_id IS NOT NULL)),
        UNIQUE (id, telegram_user_id, moderation_event_id)
    )""",
    """INSERT INTO user_warnings_v3
        (id, telegram_user_id, created_at, reason, created_by, admin_action_id,
         moderation_event_id, cleared_at, cleared_by, source, status)
        SELECT w.id, w.telegram_user_id, w.created_at, w.reason, w.created_by,
               w.admin_action_id, w.moderation_event_id, w.cleared_at, w.cleared_by,
               CASE WHEN EXISTS (SELECT 1 FROM admin_audit_log a WHERE a.id = w.admin_action_id
                   AND a.action = 'warn' AND a.admin_id = w.created_by
                   AND a.target_user_id = w.telegram_user_id) THEN 'manual_admin' ELSE 'legacy_unknown' END,
               CASE WHEN w.cleared_at IS NULL THEN 'active' ELSE 'revoked' END
        FROM user_warnings w""",
    """INSERT INTO sqlite_sequence (name, seq) SELECT 'user_warnings_v3', COALESCE(
        (SELECT seq FROM sqlite_sequence WHERE name = 'user_warnings'), 0)
        WHERE NOT EXISTS (SELECT 1 FROM sqlite_sequence WHERE name = 'user_warnings_v3')""",
    """UPDATE sqlite_sequence SET seq = MAX(seq, COALESCE(
        (SELECT seq FROM sqlite_sequence WHERE name = 'user_warnings'), 0)) WHERE name = 'user_warnings_v3'""",
    "DROP TABLE user_warnings",
    "ALTER TABLE user_warnings_v3 RENAME TO user_warnings",
    "CREATE INDEX ix_warnings_active ON user_warnings(telegram_user_id) WHERE cleared_at IS NULL",
    "CREATE INDEX ix_warnings_audit ON user_warnings(admin_action_id)",
    "CREATE INDEX ix_warnings_event ON user_warnings(moderation_event_id)",
    """CREATE INDEX ix_warnings_moderation_active ON user_warnings(telegram_user_id)
        WHERE source = 'automatic_moderation' AND status = 'active' AND cleared_at IS NULL""",
    """CREATE UNIQUE INDEX ux_warning_automatic_event ON user_warnings(moderation_event_id)
        WHERE source = 'automatic_moderation'""",
    """ALTER TABLE users ADD COLUMN block_source TEXT
        CHECK (block_source IN ('manual_admin', 'legacy_unknown'))""",
    """UPDATE users SET block_source = CASE WHEN EXISTS
        (SELECT 1 FROM admin_audit_log a WHERE a.target_user_id = users.telegram_user_id
         AND a.action = 'block' AND a.admin_id = users.blocked_by
         AND a.timestamp = users.blocked_at AND a.reason = users.block_reason)
        THEN 'manual_admin' ELSE 'legacy_unknown' END WHERE is_blocked = 1""",
    """ALTER TABLE user_restrictions ADD COLUMN source TEXT NOT NULL DEFAULT 'legacy_unknown'
        CHECK (source IN ('legacy_unknown', 'manual_admin', 'automatic_moderation'))""",
    """UPDATE user_restrictions SET source = 'manual_admin' WHERE EXISTS
        (SELECT 1 FROM admin_audit_log a WHERE a.id = user_restrictions.admin_action_id
         AND a.action = 'restrict' AND a.admin_id = user_restrictions.created_by
         AND a.target_user_id = user_restrictions.telegram_user_id)""",
    "ALTER TABLE moderation_events ADD COLUMN signal_source TEXT",
    "ALTER TABLE moderation_events ADD COLUMN provider_code TEXT",
    "ALTER TABLE moderation_events ADD COLUMN provider_severity TEXT",
    "ALTER TABLE moderation_events ADD COLUMN confidence REAL CHECK (confidence BETWEEN 0 AND 1)",
    "ALTER TABLE moderation_events ADD COLUMN warning_id INTEGER REFERENCES user_warnings(id) ON DELETE SET NULL",
    "ALTER TABLE moderation_events ADD COLUMN active_warning_count INTEGER CHECK (active_warning_count >= 0)",
    """ALTER TABLE moderation_events ADD COLUMN automatic_action TEXT NOT NULL DEFAULT 'none'
        CHECK (automatic_action IN ('none', 'warning', 'block'))""",
    """ALTER TABLE moderation_events ADD COLUMN resulting_user_state TEXT
        CHECK (resulting_user_state IN ('allowed', 'blocked', 'restricted', 'unauthorized'))""",
    """CREATE UNIQUE INDEX ux_moderation_automatic_request ON moderation_events(telegram_user_id, request_id)
        WHERE automatic_action != 'none' AND request_id IS NOT NULL""",
    """CREATE TABLE moderation_sanctions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        telegram_user_id INTEGER NOT NULL REFERENCES users(telegram_user_id),
        moderation_event_id INTEGER NOT NULL REFERENCES moderation_events(id),
        warning_id INTEGER NOT NULL,
        source TEXT NOT NULL CHECK (source = 'automatic_moderation'),
        action TEXT NOT NULL CHECK (action = 'block'),
        reason_code TEXT NOT NULL CHECK (reason_code = 'active_moderation_warnings_3'),
        created_at TEXT NOT NULL, revoked_at TEXT,
        revoked_by INTEGER REFERENCES users(telegram_user_id),
        revocation_reason TEXT,
        FOREIGN KEY (warning_id, telegram_user_id, moderation_event_id)
            REFERENCES user_warnings(id, telegram_user_id, moderation_event_id),
        CHECK ((revoked_at IS NULL AND revoked_by IS NULL AND revocation_reason IS NULL)
            OR (revoked_at IS NOT NULL AND revoked_by IS NOT NULL AND revocation_reason IS NOT NULL)),
        UNIQUE (id, telegram_user_id, moderation_event_id, warning_id)
    )""",
    "CREATE UNIQUE INDEX ux_sanction_active ON moderation_sanctions(telegram_user_id) WHERE revoked_at IS NULL",
    "CREATE INDEX ix_sanction_event ON moderation_sanctions(moderation_event_id)",
    """CREATE TABLE moderation_appeals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        telegram_user_id INTEGER NOT NULL REFERENCES users(telegram_user_id),
        moderation_event_id INTEGER NOT NULL REFERENCES moderation_events(id),
        warning_id INTEGER NOT NULL REFERENCES user_warnings(id),
        sanction_id INTEGER NOT NULL UNIQUE,
        explanation TEXT NOT NULL CHECK (length(explanation) BETWEEN 1 AND 1000),
        created_at TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected')),
        reviewed_by INTEGER REFERENCES users(telegram_user_id), reviewed_at TEXT, admin_reason TEXT,
        admin_action_id INTEGER REFERENCES admin_audit_log(id),
        resulting_warning_count INTEGER CHECK (resulting_warning_count >= 0),
        resulting_user_state TEXT,
        FOREIGN KEY (sanction_id, telegram_user_id, moderation_event_id, warning_id)
            REFERENCES moderation_sanctions(id, telegram_user_id, moderation_event_id, warning_id),
        CHECK ((status = 'pending' AND reviewed_by IS NULL AND reviewed_at IS NULL AND admin_reason IS NULL)
            OR (status != 'pending' AND reviewed_by IS NOT NULL AND reviewed_at IS NOT NULL
                AND length(admin_reason) BETWEEN 1 AND 512 AND admin_action_id IS NOT NULL))
    )""",
    "CREATE INDEX ix_appeal_status ON moderation_appeals(status, id)",
    "CREATE INDEX ix_appeal_user ON moderation_appeals(telegram_user_id, id)",
    # Extend the existing outbox/worker, including already queued Trusted notices.
    """CREATE TABLE notification_outbox_v3 (
        id INTEGER PRIMARY KEY AUTOINCREMENT, recipient_id INTEGER NOT NULL,
        application_id INTEGER REFERENCES trusted_applications(id),
        appeal_id INTEGER REFERENCES moderation_appeals(id),
        kind TEXT NOT NULL CHECK (kind IN ('trusted_pending', 'trusted_approved', 'trusted_rejected',
                                          'appeal_pending', 'appeal_approved', 'appeal_rejected')),
        created_at TEXT NOT NULL, sent_at TEXT,
        status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'sent', 'failed')),
        attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at TEXT NOT NULL, error_code TEXT,
        CHECK ((kind IN ('trusted_pending', 'trusted_approved', 'trusted_rejected')
                AND application_id IS NOT NULL AND appeal_id IS NULL)
            OR (kind IN ('appeal_pending', 'appeal_approved', 'appeal_rejected')
                AND appeal_id IS NOT NULL AND application_id IS NULL)),
        UNIQUE (recipient_id, application_id, kind), UNIQUE (recipient_id, appeal_id, kind)
    )""",
    """INSERT INTO notification_outbox_v3
        (id, recipient_id, application_id, kind, created_at, sent_at, status, attempts, next_attempt_at, error_code)
        SELECT id, recipient_id, application_id, kind, created_at, sent_at, status, attempts, next_attempt_at, error_code
        FROM notification_outbox""",
    """INSERT INTO sqlite_sequence (name, seq) SELECT 'notification_outbox_v3', COALESCE(
        (SELECT seq FROM sqlite_sequence WHERE name = 'notification_outbox'), 0)
        WHERE NOT EXISTS (SELECT 1 FROM sqlite_sequence WHERE name = 'notification_outbox_v3')""",
    """UPDATE sqlite_sequence SET seq = MAX(seq, COALESCE(
        (SELECT seq FROM sqlite_sequence WHERE name = 'notification_outbox'), 0)) WHERE name = 'notification_outbox_v3'""",
    "DROP TABLE notification_outbox",
    "ALTER TABLE notification_outbox_v3 RENAME TO notification_outbox",
    "CREATE INDEX ix_notifications_pending ON notification_outbox(status, next_attempt_at)",
)
