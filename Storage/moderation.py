"""The existing moderation repository, extended with atomic policy and appeals."""
from dataclasses import asdict

from Storage.access_helpers import (
    access_decision, active_moderation_warnings, automatic_block, queue_notification,
)
from Storage.models import ModerationEvent, ModerationOutcome
from Storage.safety_queries import save_evaluation, policy_state


WARNING_THRESHOLD = 3


def _insert_event(connection, event: ModerationEvent) -> int:
    return connection.execute(
        """INSERT INTO moderation_events
        (timestamp, telegram_user_id, request_id, model, provider, category, severity,
         source, action, code, guardrail, signal_source, provider_code, provider_severity, confidence,
         classifier_model, classifier_provider, policy_version, intent, confidence_source, policy_action,
         policy_fingerprint, safety_evaluation_id)
        VALUES (:timestamp, :telegram_user_id, :request_id, :model, :provider, :category, :severity,
                :source, :action, :code, :guardrail, :signal_source, :provider_code, :provider_severity, :confidence,
                :classifier_model, :classifier_provider, :policy_version, :intent, :confidence_source, :policy_action,
                :policy_fingerprint, :safety_evaluation_id)""",
        asdict(event)).lastrowid


def _create_block(connection, *, user_id: int, event_id: int, warning_id: int, timestamp: str) -> int:
    return connection.execute(
        """INSERT INTO moderation_sanctions
        (telegram_user_id, moderation_event_id, warning_id, source, action, reason_code, created_at)
        VALUES (?, ?, ?, 'automatic_moderation', 'block', 'active_moderation_warnings_3', ?)""",
        (user_id, event_id, warning_id, timestamp)).lastrowid


class SQLiteModeration:
    def __init__(self, database):
        self._db = database

    async def record(self, event: ModerationEvent) -> int:
        # Historical/manual observations do not implicitly award policy warnings.
        return await self._db.run(lambda c: _insert_event(c, event), write=True)

    async def policy_state(self) -> dict:
        return await self._db.run(policy_state)

    async def save_safety_evaluation(self, record: dict) -> int:
        return await self._db.run(lambda c: save_evaluation(c, record), write=True)

    async def record_refusal(self, event: ModerationEvent) -> int:
        if event.policy_action != "refuse":
            raise ValueError("Nonpunitive refusal requires REFUSE policy action")
        def operation(c):
            event_id = _insert_event(c, event)
            c.execute("UPDATE moderation_events SET review_status = 'resolved', active_warning_count = ?, resulting_user_state = ? WHERE id = ?",
                      (active_moderation_warnings(c, event.telegram_user_id), access_decision(c, event.telegram_user_id, event.timestamp).code, event_id))
            if event.safety_evaluation_id is not None:
                c.execute("UPDATE safety_evaluations SET moderation_event_id = ? WHERE id = ? AND telegram_user_id = ? AND mode = 'production'",
                          (event_id, event.safety_evaluation_id, event.telegram_user_id))
            return event_id
        return await self._db.run(operation, write=True)

    async def confirm(self, event: ModerationEvent) -> ModerationOutcome:
        if event.action != "block" or not event.signal_source or not event.request_id:
            raise ValueError("An automatic violation requires a confirmed, request-scoped signal")

        def operation(c):
            previous = c.execute(
                "SELECT * FROM moderation_events WHERE telegram_user_id = ? AND request_id = ? "
                "AND automatic_action != 'none'", (event.telegram_user_id, event.request_id)).fetchone()
            if previous:
                block = automatic_block(c, event.telegram_user_id)
                return ModerationOutcome(previous["id"], previous["warning_id"], previous["active_warning_count"],
                                         previous["automatic_action"], previous["resulting_user_state"],
                                         block["id"] if block else None, created=False)
            event_id = _insert_event(c, event)
            warning_id = c.execute(
                """INSERT INTO user_warnings
                (telegram_user_id, created_at, reason, moderation_event_id, source)
                VALUES (?, ?, 'confirmed_moderation_violation', ?, 'automatic_moderation')""",
                (event.telegram_user_id, event.timestamp, event_id)).lastrowid
            count = active_moderation_warnings(c, event.telegram_user_id)
            block = automatic_block(c, event.telegram_user_id)
            sanction_id = block["id"] if block else None
            action = "warning"
            if count >= WARNING_THRESHOLD and not block:
                sanction_id = _create_block(c, user_id=event.telegram_user_id, event_id=event_id,
                                            warning_id=warning_id, timestamp=event.timestamp)
                action = "block"
            state = access_decision(c, event.telegram_user_id, event.timestamp).code
            c.execute(
                """UPDATE moderation_events SET warning_id = ?, active_warning_count = ?, automatic_action = ?,
                    resulting_user_state = ?, review_status = 'resolved' WHERE id = ?""",
                (warning_id, count, action, state, event_id))
            if event.safety_evaluation_id is not None:
                c.execute("UPDATE safety_evaluations SET moderation_event_id = ? WHERE id = ? AND telegram_user_id = ? AND mode = 'production'",
                          (event_id, event.safety_evaluation_id, event.telegram_user_id))
            return ModerationOutcome(event_id, warning_id, count, action, state, sanction_id)
        # Event + warning + sanction + audit snapshot are one transaction.
        return await self._db.run(operation, write=True)

    async def active_warnings(self, user_id: int) -> int:
        return await self._db.run(lambda c: active_moderation_warnings(c, user_id))

    async def block_for_user(self, user_id: int, sanction_id: int | None = None) -> dict:
        def operation(c):
            block = automatic_block(c, user_id)
            if block is None or (sanction_id is not None and block["id"] != sanction_id):
                return {}
            appeal = c.execute("SELECT id, status FROM moderation_appeals WHERE sanction_id = ? AND telegram_user_id = ?",
                               (block["id"], user_id)).fetchone()
            return {**dict(block), "active_warning_count": active_moderation_warnings(c, user_id),
                    "appeal": dict(appeal) if appeal else None}
        return await self._db.run(operation)

    async def create_appeal(self, *, user_id: int, sanction_id: int, explanation: str,
                            timestamp: str, admin_ids: tuple[int, ...]) -> dict:
        def operation(c):
            # Never accept user-supplied event, warning or target user references.
            block = c.execute(
                "SELECT * FROM moderation_sanctions WHERE id = ? AND telegram_user_id = ? "
                "AND source = 'automatic_moderation' AND action = 'block' AND revoked_at IS NULL",
                (sanction_id, user_id)).fetchone()
            if block is None:
                raise ValueError("No appealable automatic block for this user")
            previous = c.execute("SELECT * FROM moderation_appeals WHERE sanction_id = ? AND telegram_user_id = ?",
                                 (sanction_id, user_id)).fetchone()
            if previous:
                return {**dict(previous), "created": False}
            appeal_id = c.execute(
                """INSERT INTO moderation_appeals
                (telegram_user_id, moderation_event_id, warning_id, sanction_id, explanation, created_at)
                VALUES (?, ?, ?, ?, ?, ?)""",
                (user_id, block["moderation_event_id"], block["warning_id"], sanction_id, explanation, timestamp)).lastrowid
            for admin_id in admin_ids:
                queue_notification(c, admin_id, None, "appeal_pending", timestamp, appeal_id=appeal_id)
            return {"id": appeal_id, "status": "pending", "created": True}
        return await self._db.run(operation, write=True)

    async def appeal_for_user(self, user_id: int, appeal_id: int) -> dict:
        def operation(c):
            row = c.execute("SELECT * FROM moderation_appeals WHERE id = ? AND telegram_user_id = ?",
                            (appeal_id, user_id)).fetchone()
            return dict(row) if row else {}
        return await self._db.run(operation)

    async def list_for_user(self, user_id: int, limit: int = 20) -> list[dict]:
        return await self._db.run(lambda c: [dict(row) for row in c.execute(
            "SELECT * FROM moderation_events WHERE telegram_user_id = ? ORDER BY id DESC LIMIT ?",
            (user_id, min(max(limit, 1), 100)))])


def review_appeal(connection, command, appeal, audit_id: int) -> dict:
    """Called only inside SQLiteAdmins' action + audit transaction."""
    c, target = connection, appeal["telegram_user_id"]
    status = "approved" if command.action == "approve_appeal" else "rejected"
    metadata = {"appeal_id": appeal["id"], "moderation_event_id": appeal["moderation_event_id"],
                "warning_id": appeal["warning_id"], "sanction_id": appeal["sanction_id"],
                "before": {"active_warning_count": active_moderation_warnings(c, target),
                           "user_state": access_decision(c, target, command.timestamp).code}}
    if status == "approved":
        c.execute(
            """UPDATE user_warnings SET status = 'revoked', cleared_at = ?, cleared_by = ?
            WHERE id = ? AND telegram_user_id = ? AND moderation_event_id = ?
            AND source = 'automatic_moderation' AND status = 'active' AND cleared_at IS NULL""",
            (command.timestamp, command.admin_id, appeal["warning_id"], target, appeal["moderation_event_id"]))
        c.execute(
            """UPDATE moderation_sanctions SET revoked_at = ?, revoked_by = ?, revocation_reason = 'appeal_approved'
            WHERE id = ? AND telegram_user_id = ? AND moderation_event_id = ? AND warning_id = ?
            AND source = 'automatic_moderation' AND revoked_at IS NULL""",
            (command.timestamp, command.admin_id, appeal["sanction_id"], target,
             appeal["moderation_event_id"], appeal["warning_id"]))
        c.execute("UPDATE moderation_events SET review_status = 'dismissed', reviewed_at = ?, admin_action_id = ? WHERE id = ?",
                  (command.timestamp, audit_id, appeal["moderation_event_id"]))
        # An additional request may already have been in flight at the third
        # warning. Overturning one decision must not bypass three other warnings.
        if active_moderation_warnings(c, target) >= WARNING_THRESHOLD and not automatic_block(c, target):
            latest = c.execute(
                "SELECT id, moderation_event_id FROM user_warnings WHERE telegram_user_id = ? "
                "AND source = 'automatic_moderation' AND status = 'active' AND cleared_at IS NULL ORDER BY id DESC LIMIT 1",
                (target,)).fetchone()
            metadata["replacement_sanction_id"] = _create_block(
                c, user_id=target, event_id=latest["moderation_event_id"], warning_id=latest["id"], timestamp=command.timestamp)
    count = active_moderation_warnings(c, target)
    state = access_decision(c, target, command.timestamp).code
    c.execute(
        """UPDATE moderation_appeals SET status = ?, reviewed_by = ?, reviewed_at = ?, admin_reason = ?,
            admin_action_id = ?, resulting_warning_count = ?, resulting_user_state = ? WHERE id = ? AND status = 'pending'""",
        (status, command.admin_id, command.timestamp, command.reason, audit_id, count, state, appeal["id"]))
    queue_notification(c, target, None, "appeal_" + status, command.timestamp, appeal_id=appeal["id"])
    metadata.update(after={"active_warning_count": count, "user_state": state}, decision=status)
    return metadata
