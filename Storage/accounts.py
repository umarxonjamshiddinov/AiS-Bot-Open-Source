from __future__ import annotations

from datetime import datetime, timedelta

from Storage.access_helpers import account_summary, approve_pending, queue_notification
from Storage.models import ActivationResult, AccessDeniedError, utc_timestamp
from Storage.repositories import _access


class SQLiteAccounts:
    def __init__(self, database, standard_limit=50, trusted_limit=500, higher_cost_limit=15):
        self._db = database
        self._standard, self._trusted = standard_limit, trusted_limit
        self._higher = higher_cost_limit

    async def snapshot(self, user_id: int, timestamp: str) -> dict:
        return await self._db.run(lambda c: account_summary(c, user_id, timestamp, self._standard, self._trusted, self._higher))

    async def apply(self, user_id: int, timestamp: str, admin_ids: tuple[int, ...]) -> dict:
        def operation(c):
            decision = _access(c, user_id, timestamp)
            if not decision.allowed:
                raise AccessDeniedError(decision)
            account = c.execute("SELECT tier FROM user_access WHERE telegram_user_id = ?", (user_id,)).fetchone()
            if account[0] == "TRUSTED":
                return {"status": "already_trusted", "created": False}
            previous = c.execute(
                "SELECT * FROM trusted_applications WHERE telegram_user_id = ? AND status = 'pending'", (user_id,)).fetchone()
            if previous:
                return {**dict(previous), "created": False}
            application_id = c.execute(
                "INSERT INTO trusted_applications (telegram_user_id, created_at) VALUES (?, ?)", (user_id, timestamp)).lastrowid
            for admin_id in admin_ids:
                queue_notification(c, admin_id, application_id, "trusted_pending", timestamp)
            return {"id": application_id, "status": "pending", "created": True}
        return await self._db.run(operation, write=True)

    async def redeem(self, *, user_id: int, purpose: str, matches: bool, configured: bool,
                     timestamp: str, max_attempts: int, window_seconds: int, cooldown_seconds: int) -> ActivationResult:
        if purpose not in {"grok", "premium"}:
            raise ValueError("Unknown activation purpose")

        def operation(c):
            decision = _access(c, user_id, timestamp)
            if not decision.allowed:
                raise AccessDeniedError(decision)
            account = c.execute("SELECT tier, premium_models_allowed FROM user_access WHERE telegram_user_id = ?", (user_id,)).fetchone()
            if (purpose == "grok" and account[1]) or (purpose == "premium" and account[0] == "TRUSTED"):
                return ActivationResult("already")
            if not configured:
                return ActivationResult("disabled")
            attempt = c.execute("SELECT * FROM activation_attempts WHERE telegram_user_id = ? AND purpose = ?",
                                (user_id, purpose)).fetchone()
            if attempt and attempt["locked_until"] and attempt["locked_until"] > timestamp:
                return ActivationResult("cooldown", 0, attempt["locked_until"])
            now = datetime.fromisoformat(timestamp)
            window_start = timestamp
            failures = 0
            if attempt and not attempt["locked_until"] and datetime.fromisoformat(attempt["window_started_at"]) + timedelta(seconds=window_seconds) > now:
                window_start, failures = attempt["window_started_at"], attempt["failure_count"]
            if matches:
                if purpose == "grok":
                    c.execute("UPDATE user_access SET premium_models_allowed = 1, grok_allowed = 1, grok_source = 'access_code', grok_granted_at = ?, "
                              "grok_granted_by = NULL WHERE telegram_user_id = ?", (timestamp, user_id))
                else:
                    c.execute("UPDATE user_access SET tier = 'TRUSTED', trusted_source = 'premium_code', trusted_at = ?, "
                              "trusted_by = NULL WHERE telegram_user_id = ?", (timestamp, user_id))
                    approve_pending(c, user_id, timestamp, reason="premium_activation")
                c.execute("DELETE FROM activation_attempts WHERE telegram_user_id = ? AND purpose = ?", (user_id, purpose))
                return ActivationResult("activated")
            failures += 1
            locked_until = utc_timestamp(now + timedelta(seconds=cooldown_seconds)) if failures >= max_attempts else None
            c.execute(
                """INSERT INTO activation_attempts VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(telegram_user_id, purpose) DO UPDATE SET
                window_started_at = excluded.window_started_at, last_attempt_at = excluded.last_attempt_at,
                failure_count = excluded.failure_count, locked_until = excluded.locked_until""",
                (user_id, purpose, window_start, timestamp, failures, locked_until))
            return ActivationResult("cooldown" if locked_until else "invalid", max(0, max_attempts - failures), locked_until)
        return await self._db.run(operation, write=True)


class SQLiteNotifications:
    def __init__(self, database):
        self._db = database

    async def pending(self, timestamp: str) -> list[dict]:
        return await self._db.run(lambda c: [dict(row) for row in c.execute(
            """SELECT n.*, COALESCE(a.telegram_user_id, p.telegram_user_id) AS telegram_user_id,
            COALESCE(a.status, p.status) AS application_status, p.resulting_warning_count, p.resulting_user_state,
            u.username, u.first_name, u.last_name
            FROM notification_outbox n LEFT JOIN trusted_applications a ON a.id = n.application_id
            LEFT JOIN moderation_appeals p ON p.id = n.appeal_id
            JOIN users u ON u.telegram_user_id = COALESCE(a.telegram_user_id, p.telegram_user_id)
            WHERE n.status = 'pending' AND n.next_attempt_at <= ? ORDER BY n.id LIMIT 20""", (timestamp,))])

    async def finish(self, notification_id: int, *, timestamp: str, status: str,
                     next_attempt_at: str, error_code: str | None) -> None:
        await self._db.run(lambda c: c.execute(
            "UPDATE notification_outbox SET status = ?, sent_at = ?, attempts = attempts + 1, "
            "next_attempt_at = ?, error_code = ? WHERE id = ? AND status = 'pending'",
            (status, timestamp if status == "sent" else None, next_attempt_at, error_code, notification_id)).rowcount, write=True)
