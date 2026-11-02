"""SQL helpers shared by repositories; all callers own the surrounding transaction."""
from Storage.models import AccessDecision
from AI.access import cost_tier_for
from AI.models import ModelCostTier


def active_moderation_warnings(connection, user_id: int) -> int:
    return connection.execute(
        "SELECT COUNT(*) FROM user_warnings WHERE telegram_user_id = ? "
        "AND source = 'automatic_moderation' AND status = 'active' AND cleared_at IS NULL",
        (user_id,)).fetchone()[0]


def automatic_block(connection, user_id: int):
    return connection.execute(
        "SELECT * FROM moderation_sanctions WHERE telegram_user_id = ? "
        "AND source = 'automatic_moderation' AND action = 'block' AND revoked_at IS NULL", (user_id,)).fetchone()


def access_decision(connection, user_id: int, timestamp: str, *, require_authorized=True) -> AccessDecision:
    user = connection.execute(
        "SELECT is_blocked, status, block_source FROM users WHERE telegram_user_id = ?", (user_id,)).fetchone()
    if user is None:
        return AccessDecision("unauthorized")
    sanction = automatic_block(connection, user_id)
    if user["is_blocked"] or sanction:
        return AccessDecision("blocked", block_source=(user["block_source"] or "legacy_unknown")
                              if user["is_blocked"] else "automatic_moderation",
                              sanction_id=sanction["id"] if sanction else None)
    restricted = connection.execute(
        "SELECT MAX(expires_at) FROM user_restrictions "
        "WHERE telegram_user_id = ? AND revoked_at IS NULL AND expires_at > ?", (user_id, timestamp)).fetchone()[0]
    if restricted:
        return AccessDecision("restricted", restricted)
    if require_authorized and user["status"] != "authorized":
        return AccessDecision("unauthorized")
    return AccessDecision("allowed")


def ensure_access(connection, user_id: int) -> None:
    connection.execute(
        "INSERT INTO user_access (telegram_user_id) VALUES (?) ON CONFLICT DO NOTHING", (user_id,))


def grok_access(connection, user_id: int, required: bool) -> AccessDecision:
    decision = premium_access(connection, user_id, required)
    if not decision.allowed:
        return AccessDecision("grok_locked")  # Legacy error code.
    return decision


def premium_access(connection, user_id: int, required: bool) -> AccessDecision:
    if required:
        row = connection.execute("SELECT premium_models_allowed FROM user_access WHERE telegram_user_id = ?", (user_id,)).fetchone()
        if row is None or not row[0]:
            return AccessDecision("premium_locked")
    return AccessDecision("allowed")


def model_access(connection, user_id: int, model_key: str) -> AccessDecision:
    tier = cost_tier_for(model_key)
    if tier is ModelCostTier.INTERNAL:
        return AccessDecision("internal_model")
    return premium_access(connection, user_id, tier is ModelCostTier.PREMIUM)


def effective_limit(connection, user_id: int, standard: int, trusted: int | None) -> int | None:
    override = connection.execute("SELECT daily_request_limit FROM user_limits WHERE telegram_user_id = ?", (user_id,)).fetchone()
    if override is not None:
        return override[0]  # A present NULL is an explicit unlimited override.
    access = connection.execute("SELECT tier FROM user_access WHERE telegram_user_id = ?", (user_id,)).fetchone()
    return trusted if access and access[0] == "TRUSTED" else standard


def requests_today(connection, user_id: int, timestamp: str) -> int:
    return connection.execute(
        "SELECT COUNT(*) FROM usage_attempts WHERE telegram_user_id = ? "
        "AND timestamp >= ? AND timestamp <= ? AND counts_toward_limit = 1",
        (user_id, timestamp[:10] + "T00:00:00.000000+00:00", timestamp[:10] + "T23:59:59.999999+00:00"),
    ).fetchone()[0]


def higher_cost_today(connection, user_id: int, timestamp: str) -> int:
    return connection.execute(
        "SELECT COUNT(*) FROM usage_attempts WHERE telegram_user_id = ? "
        "AND timestamp >= ? AND timestamp <= ? AND counts_toward_limit = 1 "
        "AND is_main_inference = 1 AND model_cost_tier = 'HIGHER_COST'",
        (user_id, timestamp[:10] + "T00:00:00.000000+00:00", timestamp[:10] + "T23:59:59.999999+00:00"),
    ).fetchone()[0]


def quota_decision(connection, user_id: int, timestamp: str, model_key: str,
                   standard: int, trusted: int | None, higher: int) -> AccessDecision:
    limit = effective_limit(connection, user_id, standard, trusted)
    if limit is not None and requests_today(connection, user_id, timestamp) >= limit:
        return AccessDecision("limit_exceeded")
    if cost_tier_for(model_key) is ModelCostTier.HIGHER_COST and higher_cost_today(connection, user_id, timestamp) >= higher:
        return AccessDecision("higher_cost_limit_exceeded")
    return AccessDecision("allowed")


def queue_notification(connection, user_id: int, application_id: int | None, kind: str,
                       timestamp: str, *, appeal_id: int | None = None) -> None:
    connection.execute(
        """INSERT INTO notification_outbox
        (recipient_id, application_id, appeal_id, kind, created_at, next_attempt_at) VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT DO NOTHING""",
        (user_id, application_id, appeal_id, kind, timestamp, timestamp))


def approve_pending(connection, user_id: int, timestamp: str, *, actor=None, audit_id=None, reason: str) -> None:
    for row in connection.execute(
        "SELECT id FROM trusted_applications WHERE telegram_user_id = ? AND status = 'pending'", (user_id,)
    ).fetchall():
        connection.execute(
            "UPDATE trusted_applications SET status = 'approved', decided_at = ?, decided_by = ?, "
            "reason = ?, admin_action_id = ? WHERE id = ?", (timestamp, actor, reason, audit_id, row[0]))
        queue_notification(connection, user_id, row[0], "trusted_approved", timestamp)


def account_summary(connection, user_id: int, timestamp: str, standard: int, trusted: int | None, higher: int = 15) -> dict:
    row = connection.execute("SELECT * FROM user_access WHERE telegram_user_id = ?", (user_id,)).fetchone()
    if row is None:
        return {}
    result = dict(row)
    override = connection.execute("SELECT daily_request_limit FROM user_limits WHERE telegram_user_id = ?", (user_id,)).fetchone()
    result["has_limit_override"] = override is not None
    result["daily_request_limit"] = override[0] if override else None
    result["effective_daily_limit"] = effective_limit(connection, user_id, standard, trusted)
    result["requests_today"] = requests_today(connection, user_id, timestamp)
    result["higher_cost_requests_today"] = higher_cost_today(connection, user_id, timestamp)
    result["higher_cost_daily_limit"] = higher
    result["active_moderation_warnings"] = active_moderation_warnings(connection, user_id)
    application = connection.execute(
        "SELECT * FROM trusted_applications WHERE telegram_user_id = ? ORDER BY id DESC LIMIT 1", (user_id,)).fetchone()
    result["application"] = dict(application) if application else None
    return result
