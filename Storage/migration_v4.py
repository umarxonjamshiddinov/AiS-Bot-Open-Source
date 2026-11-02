"""Technical input moderation accounting; applied v1-v3 are immutable."""

STATEMENTS = (
    """ALTER TABLE usage_attempts ADD COLUMN purpose TEXT NOT NULL DEFAULT 'product'
        CHECK (purpose IN ('product', 'moderation'))""",
    "CREATE INDEX ix_attempts_purpose_time ON usage_attempts(purpose, timestamp)",
    """CREATE TRIGGER moderation_attempt_no_quota_insert BEFORE INSERT ON usage_attempts
        WHEN NEW.purpose = 'moderation' AND NEW.counts_toward_limit != 0
        BEGIN SELECT RAISE(ABORT, 'Technical moderation cannot consume product quota'); END""",
    """CREATE TRIGGER moderation_attempt_no_quota_update BEFORE UPDATE OF purpose, counts_toward_limit ON usage_attempts
        WHEN NEW.purpose = 'moderation' AND NEW.counts_toward_limit != 0
        BEGIN SELECT RAISE(ABORT, 'Technical moderation cannot consume product quota'); END""",
    """ALTER TABLE usage_requests ADD COLUMN input_moderation_status TEXT
        CHECK (input_moderation_status IN ('allowed', 'blocked', 'error'))""",
    "ALTER TABLE usage_requests ADD COLUMN input_moderation_source TEXT",
    "ALTER TABLE usage_requests ADD COLUMN input_moderation_model TEXT",
    "ALTER TABLE usage_requests ADD COLUMN input_moderation_provider TEXT",
    "ALTER TABLE usage_requests ADD COLUMN input_moderation_policy TEXT",
    "ALTER TABLE usage_requests ADD COLUMN input_moderation_checked_at TEXT",
    "ALTER TABLE moderation_events ADD COLUMN classifier_model TEXT",
    "ALTER TABLE moderation_events ADD COLUMN classifier_provider TEXT",
    "ALTER TABLE moderation_events ADD COLUMN policy_version TEXT",
)
