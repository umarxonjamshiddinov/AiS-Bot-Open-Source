"""SQL operations for the existing moderation/admin repositories."""
import json
import math
from datetime import datetime, timedelta

from AI.safety_policy import MIN_SHADOW_SAMPLES, MAX_SHADOW_ERROR_RATE, POLICY_VERSION
from Storage.models import AdminResult, utc_timestamp


EVALUATION_COLUMNS = (
    "request_id", "telegram_user_id", "created_at", "mode", "origin", "policy_version", "policy_fingerprint",
    "classifier_model", "classifier_provider", "selected_model", "selected_provider", "production_policy_version", "production_action", "production_status",
    "production_latency_ms", "status", "action", "category", "intent", "severity", "confidence", "confidence_source",
    "authorization", "request_context", "reason_code", "classifier_reason_code", "model_decision", "recommended_action",
    "safe_mode", "latency_ms", "input_tokens", "output_tokens", "total_tokens", "attempt_count", "contract_retries", "error_type", "error_code",
    "is_disagreement", "context_kind", "moderation_event_id",
)


def save_evaluation(c, values: dict) -> int:
    if set(values) != set(EVALUATION_COLUMNS):
        raise ValueError("Unexpected safety evaluation fields")
    record = dict(values)
    record["is_disagreement"] = int(record["mode"] == "shadow" and record["status"] == "completed"
                                    and record["production_action"] is not None and record["action"] != record["production_action"])
    c.execute(f"INSERT INTO safety_evaluations ({','.join(EVALUATION_COLUMNS)}) VALUES ({','.join('?' for _ in EVALUATION_COLUMNS)}) "
              "ON CONFLICT(request_id, policy_fingerprint, mode) DO NOTHING", tuple(record[key] for key in EVALUATION_COLUMNS))
    return c.execute("SELECT id FROM safety_evaluations WHERE request_id = ? AND policy_fingerprint = ? AND mode = ?",
                     (record["request_id"], record["policy_fingerprint"], record["mode"])).fetchone()[0]


def policy_state(c) -> dict:
    return dict(c.execute("SELECT * FROM safety_policy_state WHERE name = 'production'").fetchone())


def readiness(c, fingerprint: str) -> dict:
    rows = dict(c.execute(
        """SELECT COUNT(*) AS comparable,
        COALESCE(SUM(is_disagreement), 0) AS disagreements,
        COALESCE(SUM(is_disagreement = 1 AND (expected_action IS NULL OR expected_action = 'unclear')), 0) AS unreviewed,
        COALESCE(SUM(expected_action IS NOT NULL AND expected_action != 'unclear'), 0) AS reviewed,
        COALESCE(SUM(expected_action IS NOT NULL AND expected_action != 'unclear' AND expected_action != action), 0) AS known_errors
        FROM safety_evaluations WHERE policy_version = ? AND policy_fingerprint = ? AND mode = 'shadow'
        AND origin = 'live' AND status = 'completed' AND production_action IS NOT NULL
        AND production_policy_version = 'ais_safety_v1'""", (POLICY_VERSION, fingerprint)).fetchone())
    rows["minimum_samples"] = MIN_SHADOW_SAMPLES
    quality = c.execute("SELECT COUNT(*) AS total, COALESCE(SUM(status = 'error'),0) AS errors FROM safety_evaluations "
                        "WHERE policy_version = ? AND policy_fingerprint = ? AND mode = 'shadow' AND origin = 'live' "
                        "AND production_policy_version = 'ais_safety_v1'", (POLICY_VERSION, fingerprint)).fetchone()
    error_rate = quality["errors"] / quality["total"] if quality["total"] else None
    rows.update(classifier_errors=quality["errors"], observed=quality["total"], error_rate=error_rate,
                maximum_error_rate=MAX_SHADOW_ERROR_RATE)
    rows["ready"] = (rows["comparable"] >= MIN_SHADOW_SAMPLES and rows["unreviewed"] == 0 and rows["known_errors"] == 0
                     and error_rate is not None and error_rate <= MAX_SHADOW_ERROR_RATE)
    return rows


def safety_summary(c, timestamp: str, fingerprint: str) -> dict:
    since = utc_timestamp(datetime.fromisoformat(timestamp) - timedelta(hours=24))
    scope = "created_at >= ? AND created_at <= ? AND policy_fingerprint = ?"
    args = (since, timestamp, fingerprint)
    result = {"policy": policy_state(c), "readiness": readiness(c, fingerprint)}
    result["actions"] = [dict(r) for r in c.execute(f"SELECT mode, action, COUNT(*) AS count FROM safety_evaluations WHERE {scope} AND status = 'completed' GROUP BY mode, action", args)]
    result["categories"] = [dict(r) for r in c.execute(f"SELECT category, COUNT(*) AS count FROM safety_evaluations WHERE {scope} AND status = 'completed' GROUP BY category ORDER BY count DESC LIMIT 8", args)]
    result["reasons"] = [dict(r) for r in c.execute(f"SELECT reason_code, COUNT(*) AS count FROM safety_evaluations WHERE {scope} AND status = 'completed' GROUP BY reason_code ORDER BY count DESC LIMIT 8", args)]
    result["status"] = [dict(r) for r in c.execute(f"SELECT mode, status, COUNT(*) AS count FROM safety_evaluations WHERE {scope} GROUP BY mode, status", args)]
    result["v4_errors"] = c.execute("SELECT COUNT(*) FROM usage_requests WHERE timestamp >= ? AND timestamp <= ? AND input_moderation_status = 'error'", (since, timestamp)).fetchone()[0]
    latencies = [r[0] for r in c.execute(f"SELECT latency_ms FROM safety_evaluations WHERE {scope} AND latency_ms IS NOT NULL AND status != 'skipped' ORDER BY latency_ms", args)]
    result["latency"] = {"count": len(latencies), "p50": latencies[math.ceil(len(latencies) * .5) - 1] if latencies else None,
                         "p95": latencies[math.ceil(len(latencies) * .95) - 1] if latencies else None}
    result["confidence_review"] = [dict(r) for r in c.execute(
        f"""SELECT CASE WHEN confidence >= .9 THEN 'high' WHEN confidence >= .6 THEN 'medium' ELSE 'low' END AS band,
        COUNT(*) AS reviewed, SUM(expected_action = action) AS matched
        FROM safety_evaluations WHERE {scope} AND confidence IS NOT NULL
        AND expected_action IS NOT NULL AND expected_action != 'unclear' GROUP BY band""", args)]
    warnings = dict(c.execute(
        """SELECT COUNT(*) AS issued, COALESCE(SUM(status != 'active' OR cleared_at IS NOT NULL), 0) AS revoked
        FROM user_warnings WHERE source = 'automatic_moderation' AND created_at >= ? AND created_at <= ?""", (since, timestamp)).fetchone())
    warnings["revoked_percent"] = round(100 * warnings["revoked"] / warnings["issued"], 1) if warnings["issued"] else None
    result["warnings"] = warnings
    result["appeal_overturns"] = c.execute("SELECT COUNT(*) FROM moderation_appeals WHERE status = 'approved' AND reviewed_at >= ? AND reviewed_at <= ?", (since, timestamp)).fetchone()[0]
    result["auto_blocks"] = c.execute("SELECT COUNT(*) FROM moderation_sanctions WHERE source = 'automatic_moderation' AND created_at >= ? AND created_at <= ?", (since, timestamp)).fetchone()[0]
    result["shadow_tokens"] = c.execute(f"SELECT SUM(total_tokens) FROM safety_evaluations WHERE {scope} AND mode = 'shadow'", args).fetchone()[0]
    result["contract_retries"] = c.execute(f"SELECT COALESCE(SUM(contract_retries),0) FROM safety_evaluations WHERE {scope}", args).fetchone()[0]
    return result


def apply_safety_command(c, command) -> AdminResult:
    before, after, target, resource_id = {}, {}, command.admin_id, None
    if command.action == "review_safety":
        row = c.execute("SELECT * FROM safety_evaluations WHERE id = ?", (command.safety_evaluation_id,)).fetchone()
        if row is None or row["status"] != "completed":
            raise ValueError("Completed evaluation is required")
        if command.target_user_id is not None and command.target_user_id != row["telegram_user_id"]:
            raise ValueError("Evaluation belongs to a different user")
        if command.expected_safety_review_id != row["review_admin_action_id"]:
            raise ValueError("Safety review changed; reload the card")
        if command.expected_safety_action not in {"allow", "safe_complete", "refuse", "violation", "unclear"}:
            raise ValueError("Invalid expected action")
        before = {"expected_action": row["expected_action"], "review_admin_action_id": row["review_admin_action_id"]}
        after = {"expected_action": command.expected_safety_action}
        target, resource_id, resource_type = row["telegram_user_id"], row["id"], "safety_evaluation"
    else:
        before = policy_state(c)
        if command.action == "activate_safety_v5":
            ready = readiness(c, command.policy_fingerprint)
            if not ready["ready"]:
                raise ValueError("Shadow evidence and human review requirements are not satisfied")
            after = {"policy_version": POLICY_VERSION, "policy_fingerprint": command.policy_fingerprint, "evidence": ready}
        elif command.action == "rollback_safety_v4":
            after = {"policy_version": "ais_safety_v1", "policy_fingerprint": None}
        else:
            raise ValueError("Unknown safety action")
        resource_type, resource_id = "safety_policy", "production"
    audit = c.execute(
        "INSERT INTO admin_audit_log (timestamp, admin_id, action, target_user_id, resource_type, resource_id, reason, metadata_json) VALUES (?,?,?,?,?,?,?,?)",
        (command.timestamp, command.admin_id, command.action, target, resource_type, str(resource_id), command.reason,
         json.dumps({"before": before, "after": after}, ensure_ascii=False, separators=(",", ":")))).lastrowid
    if command.action == "review_safety":
        c.execute("UPDATE safety_evaluations SET expected_action = ?, reviewed_by = ?, reviewed_at = ?, admin_reason = ?, review_admin_action_id = ? WHERE id = ?",
                  (command.expected_safety_action, command.admin_id, command.timestamp, command.reason, audit, resource_id))
    else:
        c.execute("UPDATE safety_policy_state SET policy_version = ?, policy_fingerprint = ?, revision = revision + 1, changed_at = ?, changed_by = ?, admin_action_id = ? WHERE name = 'production'",
                  (after["policy_version"], after["policy_fingerprint"], command.timestamp, command.admin_id, audit))
    return AdminResult(audit, target, resource_id if type(resource_id) is int else None)
