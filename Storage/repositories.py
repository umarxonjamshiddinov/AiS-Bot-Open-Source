from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from typing import TYPE_CHECKING

from Storage.access_helpers import (
    account_summary, approve_pending, effective_limit, ensure_access, grok_access, queue_notification, requests_today,
    access_decision as _access, active_moderation_warnings, automatic_block,
    premium_access, model_access, quota_decision,
)
from AI.access import cost_tier_for, selection_available, model_is_selectable
from AI.models import ModelCostTier
from AI.providers import models_for_backend, model_for_backend
from Storage.moderation import SQLiteModeration, review_appeal
from Storage.safety_queries import apply_safety_command
from Storage.admin_queries import SQLiteAdminQueries
from Storage.models import (
    AccessDecision, AccessDeniedError, AdminCommand, AdminResult, ModerationEvent,
    UserProfile, UserRecord, UserSelection,
)

if TYPE_CHECKING:
    from Storage.sqlite import SQLiteDatabase


def _ensure_user(connection, user_id: int, timestamp: str) -> None:
    # Placeholder identities allow pre-emptive bans and configured admins who
    # have not contacted Telegram yet. Observed profile/last_seen remain NULL.
    connection.execute(
        "INSERT INTO users (telegram_user_id, created_at) VALUES (?, ?) "
        "ON CONFLICT(telegram_user_id) DO NOTHING", (user_id, timestamp),
    )
    ensure_access(connection, user_id)


def _user_record(connection, user_id: int) -> UserRecord | None:
    row = connection.execute("SELECT * FROM users WHERE telegram_user_id = ?", (user_id,)).fetchone()
    if row is None:
        return None
    values = dict(row)
    block = automatic_block(connection, user_id)
    if not values["is_blocked"] and block:
        values.update(is_blocked=True, blocked_at=block["created_at"], block_reason=block["reason_code"],
                      block_source=block["source"], blocked_by=None)
    return UserRecord(**values)


class SQLiteUsers:
    def __init__(self, database: SQLiteDatabase, standard_limit=50, trusted_limit=500, higher_cost_limit=15) -> None:
        self._db = database
        self._standard, self._trusted, self._higher = standard_limit, trusted_limit, higher_cost_limit

    async def observe(self, profile: UserProfile, timestamp: str) -> UserRecord:
        def operation(connection):
            connection.execute(
                """INSERT INTO users
                    (telegram_user_id, username, first_name, last_name, created_at, last_seen_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(telegram_user_id) DO UPDATE SET
                    username = excluded.username, first_name = excluded.first_name,
                    last_name = excluded.last_name, last_seen_at = excluded.last_seen_at""",
                (profile.telegram_user_id, profile.username, profile.first_name,
                 profile.last_name, timestamp, timestamp),
            )
            ensure_access(connection, profile.telegram_user_id)
            connection.execute(
                "UPDATE users SET status = 'authorized', authorized_at = COALESCE(authorized_at, ?) "
                "WHERE telegram_user_id = ? AND status = 'pending' AND is_blocked = 0",
                (timestamp, profile.telegram_user_id))
            return _user_record(connection, profile.telegram_user_id)
        return await self._db.run(operation, write=True)

    async def get(self, user_id: int) -> UserRecord | None:
        return await self._db.run(lambda c: _user_record(c, user_id))

    async def access(self, user_id: int, timestamp: str, *, requires_grok=False, requires_premium=False) -> AccessDecision:
        def operation(connection):
            decision = _access(connection, user_id, timestamp)
            if decision.allowed and requires_premium:
                return premium_access(connection, user_id, True)
            return grok_access(connection, user_id, requires_grok) if decision.allowed else decision
        return await self._db.run(operation)

    async def authorize(self, user_id: int, timestamp: str) -> AccessDecision:
        def operation(connection):
            _ensure_user(connection, user_id, timestamp)
            decision = _access(connection, user_id, timestamp, require_authorized=False)
            if decision.allowed:
                connection.execute(
                    "UPDATE users SET status = 'authorized', authorized_at = COALESCE(authorized_at, ?) "
                    "WHERE telegram_user_id = ?", (timestamp, user_id),
                )
            return decision
        return await self._db.run(operation, write=True)

    async def selection(self, chat_id: int, user_id: int) -> UserSelection:
        def operation(connection):
            row = connection.execute(
                "SELECT selected_provider, selected_model FROM user_settings "
                "WHERE telegram_user_id = ? AND chat_id = ?", (user_id, chat_id),
            ).fetchone()
            return UserSelection(*row) if row else UserSelection()
        return await self._db.run(operation)

    async def select_model(self, chat_id: int, user_id: int,
                           selection: UserSelection, timestamp: str, *, requires_grok=False, requires_premium=False) -> bool:
        model = model_for_backend(selection.provider, selection.model_key)
        if not model_is_selectable(model):
            raise ValueError("Internal or unavailable models cannot be selected")
        def operation(connection):
            decision = _access(connection, user_id, timestamp)
            if not decision.allowed:
                raise AccessDeniedError(decision)
            permission = model_access(connection, user_id, selection.model_key)
            if not permission.allowed:
                raise AccessDeniedError(permission)
            permission = premium_access(connection, user_id, requires_premium or requires_grok)
            if not permission.allowed:
                raise AccessDeniedError(permission)
            quota = quota_decision(connection, user_id, timestamp, selection.model_key,
                                   self._standard, self._trusted, self._higher)
            if not quota.allowed:
                raise AccessDeniedError(quota)
            previous = connection.execute(
                "SELECT selected_provider, selected_model FROM user_settings WHERE telegram_user_id = ? AND chat_id = ?",
                (user_id, chat_id)).fetchone()
            connection.execute(
                """INSERT INTO user_settings VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(telegram_user_id, chat_id) DO UPDATE SET
                selected_model = excluded.selected_model,
                selected_provider = excluded.selected_provider, updated_at = excluded.updated_at""",
                (user_id, chat_id, selection.model_key, selection.provider, timestamp),
            )
            return previous is None or tuple(previous) != (selection.provider, selection.model_key)
        return await self._db.run(operation, write=True)

    async def available_models(self, user_id: int, timestamp: str, provider: str):
        def operation(connection):
            decision = _access(connection, user_id, timestamp)
            if not decision.allowed:
                raise AccessDeniedError(decision)
            account = account_summary(connection, user_id, timestamp, self._standard, self._trusted, self._higher)
            return tuple(model for model in models_for_backend(provider) if selection_available(model, account))
        return await self._db.run(operation)


def _aggregate_usage(connection, request_id: str) -> None:
    row = connection.execute(
        """SELECT COUNT(*) AS attempts,
        CASE WHEN COUNT(input_tokens) = COUNT(*) THEN SUM(input_tokens) END AS input_tokens,
        CASE WHEN COUNT(output_tokens) = COUNT(*) THEN SUM(output_tokens) END AS output_tokens,
        CASE WHEN COUNT(total_tokens) = COUNT(*) THEN SUM(total_tokens) END AS total_tokens,
        SUM(total_tokens) AS accounted_tokens,
        CASE WHEN COUNT(*) > 0 AND COUNT(total_tokens) = COUNT(*)
             AND COUNT(input_tokens) = COUNT(*) AND COUNT(output_tokens) = COUNT(*)
             THEN 1 ELSE 0 END AS usage_complete
        FROM usage_attempts WHERE request_id = ?""", (request_id,),
    ).fetchone()
    connection.execute(
        "UPDATE usage_requests SET input_tokens = ?, output_tokens = ?, total_tokens = ?, "
        "accounted_tokens = ?, usage_complete = ? WHERE request_id = ?",
        (row["input_tokens"], row["output_tokens"], row["total_tokens"],
         row["accounted_tokens"], row["usage_complete"], request_id),
    )


class SQLiteUsage:
    def __init__(self, database: SQLiteDatabase, standard_limit=50, trusted_limit=500, higher_cost_limit=15) -> None:
        self._db = database
        self._standard_limit, self._trusted_limit = standard_limit, trusted_limit
        self._higher_cost_limit = higher_cost_limit

    async def start_request(self, *, request_id: str, user_id: int, timestamp: str,
                            model: str, provider: str, requires_grok=False, requires_premium=False) -> AccessDecision:
        def operation(connection):
            _ensure_user(connection, user_id, timestamp)
            decision = _access(connection, user_id, timestamp)
            if decision.allowed:
                decision = model_access(connection, user_id, model)
            if decision.allowed:
                decision = premium_access(connection, user_id, requires_premium or requires_grok)
            if decision.allowed:
                decision = quota_decision(connection, user_id, timestamp, model,
                                          self._standard_limit, self._trusted_limit, self._higher_cost_limit)
            connection.execute(
                """INSERT INTO usage_requests
                (request_id, telegram_user_id, timestamp, finished_at, model, provider,
                 status, counts_toward_limit, error_code, requires_grok, requires_premium)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (request_id, user_id, timestamp, None if decision.allowed else timestamp,
                 model, provider, "pending" if decision.allowed else
                 (decision.code if decision.code in {"blocked", "restricted", "unauthorized", "limit_exceeded"} else "blocked"),
                 0, None if decision.allowed else decision.code, int(requires_grok),
                 int(requires_premium or requires_grok or cost_tier_for(model) is ModelCostTier.PREMIUM)),
            )
            return decision
        return await self._db.run(operation, write=True)

    async def start_attempt(self, *, attempt_id: str, request_id: str,
                            user_id: int, timestamp: str, model: str,
                            provider: str, requires_grok=False, purpose: str = "product",
                            requires_premium=False, is_main_inference=True) -> AccessDecision:
        if purpose not in {"product", "moderation"}:
            raise ValueError("Invalid inference purpose")
        def operation(connection):
            decision = _access(connection, user_id, timestamp)
            if decision.allowed:
                request = connection.execute(
                    "SELECT telegram_user_id, status, requires_premium, model FROM usage_requests WHERE request_id = ?",
                    (request_id,),
                ).fetchone()
                if request is None or request[0] != user_id or request[1] != "pending":
                    raise ValueError("Attempt requires a pending request for the same user")
                decision = model_access(connection, user_id, request[3])
                if decision.allowed:
                    decision = premium_access(connection, user_id, requires_premium or requires_grok or bool(request[2]))
                if not decision.allowed:
                    return decision
                main = purpose == "product" and is_main_inference
                if main and model != request[3]:
                    raise ValueError("Main inference must use the selected request model")
                if purpose == "product":
                    decision = quota_decision(connection, user_id, timestamp, request[3],
                                              self._standard_limit, self._trusted_limit, self._higher_cost_limit)
                    if not decision.allowed:
                        return decision
                connection.execute(
                    "INSERT INTO usage_attempts "
                    "(attempt_id, request_id, model, provider, timestamp, status, telegram_user_id, purpose, counts_toward_limit, model_cost_tier, is_main_inference) "
                    "VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?)",
                    (attempt_id, request_id, model, provider, timestamp, user_id, purpose, int(main), cost_tier_for(model).value, int(main)),
                )
                # A new attempt makes full totals unknown until it finishes.
                # Persist this before a possible crash during the inference.
                _aggregate_usage(connection, request_id)
            return decision
        return await self._db.run(operation, write=True)

    async def finish_attempt(self, *, attempt_id: str, status: str, latency_ms: int,
                             input_tokens: int | None, output_tokens: int | None,
                             total_tokens: int | None, error_type: str | None,
                             error_code: str | None, counts_toward_limit: bool = True) -> None:
        def operation(connection):
            connection.execute(
                "UPDATE usage_attempts SET status = ?, latency_ms = ?, input_tokens = ?, "
                "output_tokens = ?, total_tokens = ?, error_type = ?, error_code = ?, "
                "counts_toward_limit = CASE WHEN purpose = 'product' AND is_main_inference = 1 THEN ? ELSE 0 END "
                "WHERE attempt_id = ? AND status = 'pending'",
                (status, latency_ms, input_tokens, output_tokens, total_tokens,
                 error_type, error_code, int(counts_toward_limit), attempt_id),
            )
            row = connection.execute(
                "SELECT request_id FROM usage_attempts WHERE attempt_id = ?", (attempt_id,),
            ).fetchone()
            if row is None:
                raise ValueError("Unknown attempt")
            _aggregate_usage(connection, row[0])
        await self._db.run(operation, write=True)

    async def record_input_moderation(self, *, request_id: str, status: str, timestamp: str,
                                     source: str | None = None, model: str | None = None,
                                     provider: str | None = None, policy: str | None = None, safety_action: str | None = None) -> None:
        await self._db.run(lambda c: c.execute(
            "UPDATE usage_requests SET input_moderation_status = ?, input_moderation_checked_at = ?, "
            "input_moderation_source = ?, input_moderation_model = ?, input_moderation_provider = ?, input_moderation_policy = ?, input_policy_action = ? "
            "WHERE request_id = ? AND status = 'pending'", (status, timestamp, source, model, provider, policy, safety_action, request_id)).rowcount,
            write=True)

    async def finish_request(self, *, request_id: str, status: str, timestamp: str,
                             latency_ms: int, error_type: str | None = None,
                             error_code: str | None = None, moderation_blocked: bool = False) -> None:
        def operation(connection):
            if moderation_blocked:
                # No product answer was produced. Retain real upstream tokens,
                # including any vision helper, separately from product quota.
                connection.execute("UPDATE usage_attempts SET counts_toward_limit = 0 WHERE request_id = ?", (request_id,))
            connection.execute(
                "UPDATE usage_requests SET status = ?, finished_at = ?, latency_ms = ?, "
                "error_type = ?, error_code = ? WHERE request_id = ? AND status = 'pending'",
                (status, timestamp, latency_ms, error_type, error_code, request_id),
            )
            _aggregate_usage(connection, request_id)
        await self._db.run(operation, write=True)

    async def recover_interrupted(self, timestamp: str) -> int:
        def operation(connection):
            for row in connection.execute("SELECT request_id FROM usage_requests WHERE status = 'pending'").fetchall():
                _aggregate_usage(connection, row[0])
            connection.execute(
                "UPDATE usage_attempts SET status = 'interrupted', error_code = 'process_restart' "
                "WHERE status = 'pending'",
            )
            # Attempt totals are committed as they arrive, even if a later step crashes.
            return connection.execute(
                "UPDATE usage_requests SET status = 'interrupted', finished_at = ?, "
                "error_code = 'process_restart' WHERE status = 'pending'", (timestamp,),
            ).rowcount
        return await self._db.run(operation, write=True)


class SQLiteAdmins(SQLiteAdminQueries):
    async def apply(self, command: AdminCommand) -> AdminResult:
        return await self._db.run(lambda connection: self._apply(connection, command), write=True)

    @staticmethod
    def _apply(connection: sqlite3.Connection, command: AdminCommand) -> AdminResult:
        c = command
        if c.action in {"review_safety", "activate_safety_v5", "rollback_safety_v4"}:
            _ensure_user(connection, c.admin_id, c.timestamp)
            return apply_safety_command(connection, c)
        allowed = {"block", "unblock", "restrict", "unrestrict", "warn", "clear_warnings",
                   "set_limit", "reset_limit", "review_moderation", "grant_trusted", "revoke_trusted",
                   "grant_grok", "revoke_grok", "grant_premium", "revoke_premium", "approve_trusted", "reject_trusted", "approve_appeal", "reject_appeal"}
        if c.action not in allowed:
            raise ValueError("Unknown admin action")
        if (c.appeal_id is not None) != (c.action in {"approve_appeal", "reject_appeal"}):
            raise ValueError("Appeal reference is required only for appeal decisions")
        if c.action in {"approve_trusted", "reject_trusted"} and c.application_id is None:
            raise ValueError("Application is required")
        target = c.target_user_id
        if c.action in {"approve_appeal", "reject_appeal"}:
            appeal = connection.execute("SELECT * FROM moderation_appeals WHERE id = ?", (c.appeal_id,)).fetchone()
            if appeal is None or appeal["status"] != "pending":
                raise ValueError("Appeal was already decided or no longer exists")
            if target is not None and target != appeal["telegram_user_id"]:
                raise ValueError("Appeal does not belong to target user")
            if c.event_id is not None and c.event_id != appeal["moderation_event_id"]:
                raise ValueError("Appeal does not belong to moderation event")
            target = appeal["telegram_user_id"]
        if c.application_id is not None:
            application = connection.execute("SELECT telegram_user_id, status FROM trusted_applications WHERE id = ?",
                                             (c.application_id,)).fetchone()
            if application is None or application["status"] != "pending":
                raise ValueError("Application was already decided or no longer exists")
            if target is not None and target != application[0]:
                raise ValueError("Application does not belong to target user")
            target = application[0]
        if c.event_id is not None:
            event = connection.execute(
                "SELECT telegram_user_id, review_status FROM moderation_events WHERE id = ?",
                (c.event_id,),
            ).fetchone()
            if event is None or (target is not None and target != event[0]):
                raise ValueError("Moderation event does not belong to target user")
            target = event[0]
        if target is None:
            raise ValueError("Target user is required")
        _ensure_user(connection, c.admin_id, c.timestamp)
        _ensure_user(connection, target, c.timestamp)
        before = dict(connection.execute(
            "SELECT is_blocked, block_reason FROM users WHERE telegram_user_id = ?", (target,),
        ).fetchone()) if c.action in {"block", "unblock"} else {}
        resource_type = "trusted_application" if c.application_id is not None else (
            "moderation_event" if c.action == "review_moderation" else "user")
        target_resource = c.application_id if c.application_id is not None else (c.event_id if c.action == "review_moderation" else target)
        if c.appeal_id is not None:
            resource_type, target_resource = "moderation_appeal", c.appeal_id
        metadata = {"moderation_event_id": c.event_id} if c.event_id else {}
        if c.application_id is not None:
            metadata["application_id"] = c.application_id
        if c.action in {"grant_trusted", "revoke_trusted", "grant_grok", "revoke_grok", "grant_premium", "revoke_premium", "approve_trusted"}:
            metadata["before"] = dict(connection.execute(
                "SELECT tier, trusted_source, grok_allowed, grok_source, premium_models_allowed FROM user_access WHERE telegram_user_id = ?", (target,)).fetchone())
        if before:
            metadata["before"] = before
        audit_id = connection.execute(
            """INSERT INTO admin_audit_log
            (timestamp, admin_id, action, target_user_id, resource_type, resource_id, reason)
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (c.timestamp, c.admin_id, c.action, target, resource_type,
             str(target_resource), c.reason),
        ).lastrowid
        resource_id = None
        if c.action in {"approve_appeal", "reject_appeal"}:
            metadata.update(review_appeal(connection, c, appeal, audit_id))
            resource_id = c.appeal_id
        elif c.action in {"grant_trusted", "approve_trusted"}:
            source = "admin_approval" if c.action == "approve_trusted" else "manual_admin"
            connection.execute("UPDATE user_access SET tier = 'TRUSTED', trusted_source = ?, trusted_at = ?, trusted_by = ? "
                               "WHERE telegram_user_id = ?", (source, c.timestamp, c.admin_id, target))
            approve_pending(connection, target, c.timestamp, actor=c.admin_id, audit_id=audit_id, reason=c.reason)
            resource_id = c.application_id
            metadata["after"] = {"tier": "TRUSTED", "source": source}
        elif c.action == "revoke_trusted":
            connection.execute("UPDATE user_access SET tier = 'STANDARD', trusted_source = NULL, trusted_at = NULL, "
                               "trusted_by = NULL WHERE telegram_user_id = ?", (target,))
            metadata["after"] = {"tier": "STANDARD"}
        elif c.action in {"grant_grok", "grant_premium"}:
            connection.execute("UPDATE user_access SET premium_models_allowed = 1, grok_allowed = 1, grok_source = 'manual_admin', grok_granted_at = ?, "
                               "grok_granted_by = ? WHERE telegram_user_id = ?", (c.timestamp, c.admin_id, target))
            metadata["after"] = {"premium_models_allowed": True, "grok_allowed": True}
            metadata["permission"] = "premium_models_allowed"
        elif c.action in {"revoke_grok", "revoke_premium"}:
            connection.execute("UPDATE user_access SET premium_models_allowed = 0, grok_allowed = 0, grok_source = NULL, grok_granted_at = NULL, "
                               "grok_granted_by = NULL WHERE telegram_user_id = ?", (target,))
            metadata["after"] = {"premium_models_allowed": False, "grok_allowed": False}
            metadata["permission"] = "premium_models_allowed"
        elif c.action == "reject_trusted":
            if c.application_id is None:
                raise ValueError("Application is required")
            connection.execute("UPDATE trusted_applications SET status = 'rejected', decided_at = ?, decided_by = ?, reason = ?, "
                               "admin_action_id = ? WHERE id = ?", (c.timestamp, c.admin_id, c.reason, audit_id, c.application_id))
            queue_notification(connection, target, c.application_id, "trusted_rejected", c.timestamp)
            resource_id = c.application_id
        elif c.action == "reset_limit":
            previous = connection.execute("SELECT daily_request_limit FROM user_limits WHERE telegram_user_id = ?", (target,)).fetchone()
            metadata.update(before=previous[0] if previous else "tier_default", after="tier_default")
            connection.execute("DELETE FROM user_limits WHERE telegram_user_id = ?", (target,))
        elif c.action == "block":
            connection.execute(
                "UPDATE users SET is_blocked = 1, blocked_at = ?, block_reason = ?, blocked_by = ?, block_source = 'manual_admin' "
                "WHERE telegram_user_id = ?", (c.timestamp, c.reason, c.admin_id, target),
            )
        elif c.action == "unblock":
            connection.execute(
                "UPDATE users SET is_blocked = 0, blocked_at = NULL, block_reason = NULL, "
                "blocked_by = NULL, block_source = NULL WHERE telegram_user_id = ?", (target,),
            )
        elif c.action == "restrict":
            resource_id = connection.execute(
                """INSERT INTO user_restrictions
                (telegram_user_id, created_at, expires_at, reason, created_by,
                 admin_action_id, moderation_event_id, source) VALUES (?, ?, ?, ?, ?, ?, ?, 'manual_admin')""",
                (target, c.timestamp, c.expires_at, c.reason, c.admin_id, audit_id, c.event_id),
            ).lastrowid
            metadata.update(restriction_id=resource_id, expires_at=c.expires_at)
        elif c.action == "unrestrict":
            ids = [row[0] for row in connection.execute(
                "SELECT id FROM user_restrictions WHERE telegram_user_id = ? "
                "AND revoked_at IS NULL AND expires_at > ?", (target, c.timestamp),
            )]
            connection.execute(
                "UPDATE user_restrictions SET revoked_at = ?, revoked_by = ? "
                "WHERE telegram_user_id = ? AND revoked_at IS NULL AND expires_at > ?",
                (c.timestamp, c.admin_id, target, c.timestamp),
            )
            metadata["restriction_ids"] = ids
        elif c.action == "warn":
            resource_id = connection.execute(
                """INSERT INTO user_warnings
                (telegram_user_id, created_at, reason, created_by, admin_action_id, moderation_event_id, source)
                VALUES (?, ?, ?, ?, ?, ?, 'manual_admin')""",
                (target, c.timestamp, c.reason, c.admin_id, audit_id, c.event_id),
            ).lastrowid
            metadata["warning_id"] = resource_id
        elif c.action == "clear_warnings":
            ids = [row[0] for row in connection.execute(
                "SELECT id FROM user_warnings WHERE telegram_user_id = ? AND cleared_at IS NULL AND source != 'automatic_moderation'",
                (target,),
            )]
            connection.execute(
                "UPDATE user_warnings SET status = 'revoked', cleared_at = ?, cleared_by = ? "
                "WHERE telegram_user_id = ? AND cleared_at IS NULL AND source != 'automatic_moderation'", (c.timestamp, c.admin_id, target),
            )
            metadata["warning_ids"] = ids
        elif c.action == "set_limit":
            previous = connection.execute(
                "SELECT daily_request_limit FROM user_limits WHERE telegram_user_id = ?", (target,),
            ).fetchone()
            metadata.update(before=previous[0] if previous else None, after=c.daily_request_limit)
            connection.execute(
                """INSERT INTO user_limits VALUES (?, ?, ?, ?)
                ON CONFLICT(telegram_user_id) DO UPDATE SET
                daily_request_limit = excluded.daily_request_limit,
                updated_at = excluded.updated_at, updated_by = excluded.updated_by""",
                (target, c.daily_request_limit, c.timestamp, c.admin_id),
            )
        elif c.action == "review_moderation":
            if c.event_id is None:
                raise ValueError("Moderation event is required")
            metadata.update(before=event["review_status"], after=c.review_status)
            connection.execute(
                "UPDATE moderation_events SET review_status = ?, reviewed_at = ? WHERE id = ?",
                (c.review_status, c.timestamp, c.event_id),
            )
            resource_id = c.event_id
        if c.event_id is not None:
            connection.execute(
                "UPDATE moderation_events SET admin_action_id = ? WHERE id = ?", (audit_id, c.event_id),
            )
        connection.execute(
            "UPDATE admin_audit_log SET metadata_json = ? WHERE id = ?",
            (json.dumps(metadata, ensure_ascii=False, separators=(",", ":")), audit_id),
        )
        return AdminResult(audit_id, target, resource_id)

    async def user_overview(self, user_id: int, timestamp: str) -> dict:
        def operation(connection):
            user = connection.execute("SELECT * FROM users WHERE telegram_user_id = ?", (user_id,)).fetchone()
            if user is None:
                return {}
            result = dict(user)
            result["manual_blocked"] = bool(user["is_blocked"])
            block = automatic_block(connection, user_id)
            result["automatic_block"] = dict(block) if block else None
            result["is_blocked"] = bool(user["is_blocked"] or block)
            result.update(account_summary(connection, user_id, timestamp, self._standard_limit, self._trusted_limit, self._higher_cost_limit))
            result["access"] = asdict(_access(connection, user_id, timestamp))
            result["restriction_until"] = connection.execute(
                "SELECT MAX(expires_at) FROM user_restrictions WHERE telegram_user_id = ? AND revoked_at IS NULL AND expires_at > ?",
                (user_id, timestamp)).fetchone()[0]
            result["active_warnings"] = connection.execute(
                "SELECT COUNT(*) FROM user_warnings WHERE telegram_user_id = ? AND cleared_at IS NULL AND status = 'active'",
                (user_id,),
            ).fetchone()[0]
            limit = connection.execute(
                "SELECT daily_request_limit FROM user_limits WHERE telegram_user_id = ?", (user_id,),
            ).fetchone()
            result["daily_request_limit"] = limit[0] if limit else None
            result["usage"] = [dict(row) for row in connection.execute(
                "SELECT status, COUNT(*) AS requests, SUM(accounted_tokens) AS known_tokens "
                "FROM usage_requests WHERE telegram_user_id = ? GROUP BY status", (user_id,),
            )]
            result["moderation"] = [dict(row) for row in connection.execute(
                "SELECT action, COUNT(*) AS events FROM moderation_events WHERE telegram_user_id = ? GROUP BY action", (user_id,))]
            return result
        return await self._db.run(operation)

    async def audit_for_user(self, user_id: int, limit: int = 20) -> list[dict]:
        return await self._db.run(lambda connection: [dict(row) for row in connection.execute(
            "SELECT * FROM admin_audit_log WHERE target_user_id = ? ORDER BY id DESC LIMIT ?",
            (user_id, min(max(limit, 1), 100)),
        )])

    async def counts(self) -> dict[str, int]:
        return await self._db.run(lambda connection: dict(connection.execute(
            "SELECT COUNT(*) AS users, COALESCE(SUM(status = 'authorized'), 0) AS authorized_users, "
            "COALESCE(SUM(is_blocked OR EXISTS (SELECT 1 FROM moderation_sanctions s "
            "WHERE s.telegram_user_id = users.telegram_user_id AND s.revoked_at IS NULL)), 0) AS blocked_users FROM users",
        ).fetchone()))
