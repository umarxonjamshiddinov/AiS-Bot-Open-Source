from __future__ import annotations

import asyncio
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, TypeVar

from Storage.errors import StorageError
from Storage.lease import RuntimeLease
from Storage.migrations import MIGRATIONS, migrate


T = TypeVar("T")


class SQLiteDatabase:
    """One connection, owned exclusively by one worker thread.

    Whole repository transactions are submitted as jobs, so coroutines cannot
    interleave statements inside a transaction. Cancellation never cancels a
    commit already running on the worker. No cursor/connection escapes it.
    """

    def __init__(self, path: Path, busy_timeout_ms: int = 5000) -> None:
        if busy_timeout_ms < 1:
            raise ValueError("busy_timeout_ms must be positive")
        self.path = Path(path).expanduser().resolve()
        self.busy_timeout_ms = busy_timeout_ms
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ais-sqlite")
        self._connection: sqlite3.Connection | None = None
        self._closed = False

    async def _submit(self, operation: Callable[[], T]) -> T:
        if self._closed:
            raise StorageError("Database is closed")
        future = asyncio.get_running_loop().run_in_executor(self._executor, operation)
        try:
            return await asyncio.shield(future)
        except (sqlite3.Error, OSError) as error:
            raise StorageError("SQLite operation failed") from error

    async def open(self) -> None:
        def initialize() -> None:
            if self._connection is not None:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(
                self.path, timeout=self.busy_timeout_ms / 1000, isolation_level=None,
            )
            try:
                connection.row_factory = sqlite3.Row
                connection.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms:d}")
                connection.execute("PRAGMA foreign_keys = ON")
                if connection.execute("PRAGMA journal_mode = WAL").fetchone()[0] != "wal":
                    raise StorageError("WAL mode is required; use a local writable filesystem")
                connection.execute("PRAGMA synchronous = FULL")
                connection.execute("PRAGMA secure_delete = ON")
                connection.execute("PRAGMA trusted_schema = OFF")
                version = connection.execute("PRAGMA user_version").fetchone()[0]
                if 0 < version < len(MIGRATIONS):
                    from Storage.maintenance import backup_database
                    from uuid import uuid4
                    backup = self.path.parent / "backups" / f"{self.path.name}.before-v{version + 1}-{uuid4().hex}.sqlite3"
                    backup_database(self.path, backup)  # Includes source integrity/FK/ledger checks.
                migrate(connection)
                integrity = [tuple(row) for row in connection.execute("PRAGMA integrity_check")]
                if integrity != [("ok",)] or connection.execute("PRAGMA foreign_key_check").fetchone():
                    raise StorageError("Database integrity check failed after migration")
            except BaseException:
                connection.close()
                raise
            self._connection = connection

        await self._submit(initialize)

    async def run(self, operation: Callable[[sqlite3.Connection], T], *, write=False) -> T:
        def execute() -> T:
            if self._connection is None:
                raise StorageError("Database was not initialized")
            connection = self._connection
            if write:
                connection.execute("BEGIN IMMEDIATE")
            try:
                result = operation(connection)
                if write:
                    connection.commit()
                return result
            except BaseException:
                if write:
                    connection.rollback()
                raise

        return await self._submit(execute)

    async def close(self) -> None:
        if self._closed:
            return

        def close_connection() -> None:
            if self._connection is not None:
                self._connection.close()
                self._connection = None

        try:
            await self._submit(close_connection)
        finally:
            self._closed = True
            await asyncio.to_thread(self._executor.shutdown, wait=True)


class SQLiteStorage:
    def __init__(self, path: Path, busy_timeout_ms: int = 5000, *, standard_limit=50, trusted_limit=500, higher_cost_limit=15) -> None:
        from Storage.repositories import (
            SQLiteAdmins, SQLiteModeration, SQLiteUsage, SQLiteUsers,
        )
        from Storage.accounts import SQLiteAccounts, SQLiteNotifications
        self.database = SQLiteDatabase(path, busy_timeout_ms)
        self._lease = RuntimeLease(self.database.path)
        self.users = SQLiteUsers(self.database, standard_limit, trusted_limit, higher_cost_limit)
        self.moderation = SQLiteModeration(self.database)
        self.usage = SQLiteUsage(self.database, standard_limit, trusted_limit, higher_cost_limit)
        self.admins = SQLiteAdmins(self.database, standard_limit, trusted_limit, higher_cost_limit)
        self.accounts = SQLiteAccounts(self.database, standard_limit, trusted_limit, higher_cost_limit)
        self.notifications = SQLiteNotifications(self.database)

    async def open(self) -> None:
        await self.database.open()

    async def acquire_runtime(self) -> None:
        await self.database._submit(self._lease.acquire)

    async def close(self) -> None:
        try:
            await self.database.close()
        finally:
            await asyncio.to_thread(self._lease.release)

    async def purge(self, *, usage_before: str, moderation_before: str,
                    audit_before: str, batch_size: int = 500) -> dict[str, int]:
        """Bounded transactions; yield between batches during routine maintenance."""
        counts = {}
        # Identifiers below are internal constants, never user input.
        for table, key, time_column, cutoff, condition in (
            ("usage_requests", "request_id", "timestamp", usage_before, "AND status != 'pending'"),
            ("moderation_events", "id", "timestamp", moderation_before, "AND automatic_action = 'none' "
             "AND NOT EXISTS (SELECT 1 FROM moderation_sanctions s WHERE s.moderation_event_id = moderation_events.id)"),
            ("safety_evaluations", "id", "created_at", moderation_before,
             "AND NOT EXISTS (SELECT 1 FROM moderation_events m WHERE m.safety_evaluation_id = safety_evaluations.id)"),
            ("admin_audit_log", "id", "timestamp", audit_before,
             "AND NOT EXISTS (SELECT 1 FROM moderation_appeals a WHERE a.admin_action_id = admin_audit_log.id) "
             "AND NOT EXISTS (SELECT 1 FROM safety_evaluations s WHERE s.review_admin_action_id = admin_audit_log.id) "
             "AND NOT EXISTS (SELECT 1 FROM safety_policy_state p WHERE p.admin_action_id = admin_audit_log.id)"),
        ):
            total = 0
            while True:
                def delete_batch(connection):
                    return connection.execute(
                        f"DELETE FROM {table} WHERE {key} IN "
                        f"(SELECT {key} FROM {table} WHERE {time_column} < ? {condition} LIMIT ?)",
                        (cutoff, batch_size),
                    ).rowcount
                count = await self.database.run(delete_batch, write=True)
                total += count
                if count < batch_size:
                    break
                await asyncio.sleep(0)
            counts[table] = total
        return counts
