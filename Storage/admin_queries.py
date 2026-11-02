from Storage.models import Page
from Storage.safety_queries import safety_summary


class SQLiteAdminQueries:
    def __init__(self, database, standard_limit=50, trusted_limit=500, higher_cost_limit=15):
        self._db = database
        self._standard_limit, self._trusted_limit = standard_limit, trusted_limit
        self._higher_cost_limit = higher_cost_limit

    async def page(self, resource: str, page: int = 0, *, user_id: int | None = None,
                   filter_value: str = "all", search: str = "", page_size: int = 8) -> Page:
        sources = {
            "users": ("users u JOIN user_access a USING(telegram_user_id)", "u.*, a.tier, a.grok_allowed, a.premium_models_allowed", "u.telegram_user_id", "u.created_at DESC, u.telegram_user_id DESC"),
            "events": ("moderation_events a JOIN users u USING(telegram_user_id)", "a.*, u.username, u.first_name, u.last_name", "a.id", "a.id DESC"),
            "applications": ("trusted_applications a JOIN users u USING(telegram_user_id)", "a.*, u.username, u.first_name, u.last_name", "a.id", "a.id DESC"),
            "appeals": ("moderation_appeals a JOIN users u USING(telegram_user_id)", "a.*, u.username, u.first_name, u.last_name", "a.id", "a.id DESC"),
            "safety": ("safety_evaluations a JOIN users u USING(telegram_user_id)", "a.*, u.username, u.first_name, u.last_name", "a.id", "a.id DESC"),
            "audit": ("admin_audit_log a LEFT JOIN users u ON u.telegram_user_id = a.target_user_id", "a.*, u.username, u.first_name, u.last_name", "a.id", "a.id DESC"),
            "usage": ("usage_requests a", "a.*", "a.request_id", "a.timestamp DESC, a.request_id"),
        }
        if resource not in sources:
            raise ValueError("Unknown admin resource")
        source, columns, _, order = sources[resource]
        conditions, args = [], []
        if user_id is not None:
            column = "a.target_user_id" if resource == "audit" else ("u.telegram_user_id" if resource == "users" else "a.telegram_user_id")
            conditions.append(column + " = ?")
            args.append(user_id)
        filters = {
            ("users", "blocked"): "(u.is_blocked = 1 OR EXISTS (SELECT 1 FROM moderation_sanctions s WHERE s.telegram_user_id = u.telegram_user_id AND s.revoked_at IS NULL))",
            ("users", "trusted"): "a.tier = 'TRUSTED'",
            ("users", "grok"): "a.premium_models_allowed = 1",
            ("users", "premium"): "a.premium_models_allowed = 1",
            ("events", "open"): "a.review_status = 'open'",
            ("applications", "pending"): "a.status = 'pending'",
            ("appeals", "pending"): "a.status = 'pending'",
            ("safety", "pending"): "a.is_disagreement = 1 AND (a.expected_action IS NULL OR a.expected_action = 'unclear')",
            ("safety", "disagreements"): "a.is_disagreement = 1",
            ("safety", "errors"): "a.status != 'completed'",
            ("safety", "shadow"): "a.mode = 'shadow'",
            ("safety", "production"): "a.mode = 'production'",
        }
        if filter_value != "all":
            try:
                conditions.append(filters[(resource, filter_value)])
            except KeyError as error:
                raise ValueError("Unknown list filter") from error
        if search and resource == "users":
            term = search.strip().lstrip("@")[:80]
            escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            conditions.append("(u.username LIKE ? ESCAPE '\\' OR COALESCE(u.first_name, '') || ' ' || COALESCE(u.last_name, '') LIKE ? ESCAPE '\\' OR CAST(u.telegram_user_id AS TEXT) = ?)")
            args.extend((f"%{escaped}%", f"%{escaped}%", term))
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        page_size = min(20, max(1, page_size))

        def operation(c):
            total = c.execute(f"SELECT COUNT(*) FROM {source}{where}", args).fetchone()[0]
            index = min(max(0, page), max(0, (total - 1) // page_size))
            rows = c.execute(f"SELECT {columns} FROM {source}{where} ORDER BY {order} LIMIT ? OFFSET ?",
                             (*args, page_size, index * page_size)).fetchall()
            return Page([dict(row) for row in rows], index, total, page_size)
        return await self._db.run(operation)

    async def detail(self, resource: str, resource_id: int) -> dict:
        tables = {"events": "moderation_events", "applications": "trusted_applications", "audit": "admin_audit_log",
                  "appeals": "moderation_appeals", "safety": "safety_evaluations"}
        if resource not in tables:
            raise ValueError("Unknown admin resource")

        def operation(c):
            row = c.execute(f"SELECT * FROM {tables[resource]} WHERE id = ?", (resource_id,)).fetchone()
            if row is None:
                return {}
            result = dict(row)
            if resource == "appeals":
                result["event"] = dict(c.execute("SELECT * FROM moderation_events WHERE id = ?", (row["moderation_event_id"],)).fetchone())
                result["sanction"] = dict(c.execute("SELECT * FROM moderation_sanctions WHERE id = ?", (row["sanction_id"],)).fetchone())
                result["history"] = [dict(entry) for entry in c.execute(
                    "SELECT id, timestamp, code, automatic_action, review_status FROM moderation_events "
                    "WHERE telegram_user_id = ? ORDER BY id DESC LIMIT 3", (row["telegram_user_id"],))]
            return result
        return await self._db.run(operation)

    async def safety_summary(self, timestamp: str, fingerprint: str) -> dict:
        return await self._db.run(lambda c: safety_summary(c, timestamp, fingerprint))

    async def dashboard(self, timestamp: str) -> dict:
        def operation(c):
            result = dict(c.execute(
                """SELECT COUNT(*) AS users, COALESCE(SUM(u.is_blocked OR EXISTS
                    (SELECT 1 FROM moderation_sanctions s WHERE s.telegram_user_id = u.telegram_user_id AND s.revoked_at IS NULL)), 0) AS blocked,
                COALESCE(SUM(a.tier = 'TRUSTED'), 0) AS trusted, COALESCE(SUM(a.grok_allowed), 0) AS grok
                FROM users u JOIN user_access a USING(telegram_user_id)""").fetchone())
            result["pending_applications"] = c.execute("SELECT COUNT(*) FROM trusted_applications WHERE status = 'pending'").fetchone()[0]
            result["premium"] = c.execute("SELECT COUNT(*) FROM user_access WHERE premium_models_allowed = 1").fetchone()[0]
            result["pending_appeals"] = c.execute("SELECT COUNT(*) FROM moderation_appeals WHERE status = 'pending'").fetchone()[0]
            result["open_events"] = c.execute("SELECT COUNT(*) FROM moderation_events WHERE review_status = 'open'").fetchone()[0]
            result["today"] = dict(c.execute(
                """SELECT COALESCE(SUM(counts_toward_limit), 0) AS inferences, SUM(total_tokens) AS known_tokens,
                COALESCE(SUM(status = 'error'), 0) AS errors FROM usage_attempts WHERE timestamp >= ? AND timestamp <= ?""",
                (timestamp[:10] + "T00:00:00.000000+00:00", timestamp[:10] + "T23:59:59.999999+00:00")).fetchone())
            return result
        return await self._db.run(operation)
