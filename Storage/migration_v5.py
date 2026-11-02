"""Append-only adaptive evaluations, human review and controlled policy rollout."""

STATEMENTS = (
    """CREATE TABLE safety_evaluations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        request_id TEXT REFERENCES usage_requests(request_id) ON DELETE SET NULL,
        telegram_user_id INTEGER NOT NULL REFERENCES users(telegram_user_id),
        created_at TEXT NOT NULL,
        mode TEXT NOT NULL CHECK (mode IN ('shadow', 'production')),
        origin TEXT NOT NULL CHECK (origin IN ('live', 'smoke')),
        policy_version TEXT NOT NULL, policy_fingerprint TEXT NOT NULL CHECK (length(policy_fingerprint) = 64),
        classifier_model TEXT NOT NULL, classifier_provider TEXT NOT NULL,
        selected_model TEXT, selected_provider TEXT,
        production_policy_version TEXT, production_action TEXT
            CHECK (production_action IN ('allow', 'safe_complete', 'refuse', 'violation')),
        production_status TEXT, production_latency_ms INTEGER CHECK (production_latency_ms >= 0),
        status TEXT NOT NULL CHECK (status IN ('completed', 'error', 'skipped')),
        action TEXT CHECK (action IN ('allow', 'safe_complete', 'refuse', 'violation')),
        category TEXT, intent TEXT, severity TEXT,
        confidence REAL CHECK (confidence BETWEEN 0 AND 1),
        confidence_source TEXT NOT NULL CHECK (confidence_source IN ('model_estimate', 'unavailable')),
        authorization TEXT, request_context TEXT, reason_code TEXT, classifier_reason_code TEXT,
        model_decision TEXT, recommended_action TEXT, safe_mode TEXT,
        latency_ms INTEGER CHECK (latency_ms >= 0),
        input_tokens INTEGER CHECK (input_tokens >= 0), output_tokens INTEGER CHECK (output_tokens >= 0),
        total_tokens INTEGER CHECK (total_tokens >= 0), attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
        contract_retries INTEGER NOT NULL DEFAULT 0 CHECK (contract_retries >= 0),
        error_type TEXT, error_code TEXT,
        is_disagreement INTEGER NOT NULL DEFAULT 0 CHECK (is_disagreement IN (0,1)),
        context_kind TEXT NOT NULL CHECK (context_kind IN ('text', 'document', 'image')),
        moderation_event_id INTEGER REFERENCES moderation_events(id) ON DELETE SET NULL,
        expected_action TEXT CHECK (expected_action IN ('allow', 'safe_complete', 'refuse', 'violation', 'unclear')),
        reviewed_by INTEGER REFERENCES users(telegram_user_id), reviewed_at TEXT, admin_reason TEXT,
        review_admin_action_id INTEGER REFERENCES admin_audit_log(id) ON DELETE SET NULL,
        CHECK (status != 'completed' OR action IS NOT NULL),
        CHECK ((confidence IS NULL AND confidence_source = 'unavailable') OR
               (confidence IS NOT NULL AND confidence_source = 'model_estimate')),
        CHECK ((expected_action IS NULL AND reviewed_by IS NULL AND reviewed_at IS NULL AND admin_reason IS NULL)
            OR (expected_action IS NOT NULL AND reviewed_by IS NOT NULL AND reviewed_at IS NOT NULL
                AND length(admin_reason) BETWEEN 1 AND 512)),
        UNIQUE (request_id, policy_fingerprint, mode)
    )""",
    "CREATE INDEX ix_safety_policy_time ON safety_evaluations(policy_fingerprint, mode, created_at)",
    "CREATE INDEX ix_safety_disagreement ON safety_evaluations(is_disagreement, expected_action, id)",
    "CREATE INDEX ix_safety_user_time ON safety_evaluations(telegram_user_id, created_at)",
    "CREATE INDEX ix_safety_event ON safety_evaluations(moderation_event_id)",
    "CREATE INDEX ix_safety_review_audit ON safety_evaluations(review_admin_action_id)",
    """CREATE TABLE safety_policy_state (
        name TEXT PRIMARY KEY CHECK (name = 'production'),
        policy_version TEXT NOT NULL CHECK (policy_version IN ('ais_safety_v1', 'ais_safety_v2')),
        policy_fingerprint TEXT, revision INTEGER NOT NULL DEFAULT 0,
        changed_at TEXT, changed_by INTEGER REFERENCES users(telegram_user_id),
        admin_action_id INTEGER REFERENCES admin_audit_log(id) ON DELETE SET NULL,
        CHECK (policy_version != 'ais_safety_v2' OR length(policy_fingerprint) = 64)
    )""",
    "INSERT INTO safety_policy_state(name, policy_version) VALUES ('production', 'ais_safety_v1')",
    "ALTER TABLE moderation_events ADD COLUMN intent TEXT",
    "ALTER TABLE moderation_events ADD COLUMN confidence_source TEXT",
    "ALTER TABLE moderation_events ADD COLUMN policy_action TEXT CHECK (policy_action IN ('allow', 'safe_complete', 'refuse', 'violation'))",
    "ALTER TABLE moderation_events ADD COLUMN policy_fingerprint TEXT",
    "ALTER TABLE moderation_events ADD COLUMN safety_evaluation_id INTEGER REFERENCES safety_evaluations(id) ON DELETE SET NULL",
    "ALTER TABLE usage_requests ADD COLUMN input_policy_action TEXT CHECK (input_policy_action IN ('allow', 'safe_complete', 'refuse', 'violation'))",
)
