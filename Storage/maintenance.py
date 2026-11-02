"""Local administrator tools using SQLite's consistent online backup API.

python -m Storage.maintenance backup --output backups/ais-20261001.sqlite3
python -m Storage.maintenance verify --source backups/ais-20261001.sqlite3
python -m Storage.maintenance restore --source backups/ais-20261001.sqlite3 --output data/restored.sqlite3
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path
from uuid import uuid4

from dotenv import load_dotenv

from config import PROJECT_ROOT, database_path
from Storage.migrations import MIGRATIONS
from Storage.sqlite import SQLiteStorage


def _read_only(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    connection.execute("PRAGMA query_only = ON")
    connection.execute("PRAGMA trusted_schema = OFF")
    return connection


def verify_database(path: Path) -> dict[str, int | str]:
    with closing(_read_only(path)) as connection:
        integrity = connection.execute("PRAGMA integrity_check").fetchall()
        if integrity != [("ok",)] or connection.execute("PRAGMA foreign_key_check").fetchone():
            raise RuntimeError("Database integrity or foreign key check failed")
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        rows = connection.execute(
            "SELECT version, name, checksum FROM schema_migrations ORDER BY version"
        ).fetchall()
        expected = [(m.version, m.name, m.checksum) for m in MIGRATIONS[:version]]
        if version < 1 or version > len(MIGRATIONS) or rows != expected:
            raise RuntimeError("Database migration ledger does not match this application")
        return {"path": str(path.resolve()), "schema_version": version, "integrity": "ok"}


def backup_database(source: Path, output: Path) -> dict[str, int | str]:
    """Create a verified snapshot; refuse to overwrite any existing output.

    Source connection is independent of the bot's worker. Publish the verified
    temporary file atomically with an exclusive hard link in the same directory.
    Supported on local NTFS and standard Linux filesystems.
    """
    source, output = source.expanduser().resolve(), output.expanduser().resolve()
    if source == output or output.exists():
        raise FileExistsError("Choose a new backup/restore output path")
    if not source.is_file():
        raise FileNotFoundError(source)
    verify_database(source)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.parent / f".tmp_backup_{uuid4().hex}"
    source_connection = _read_only(source)
    target_connection = None
    try:
        target_connection = sqlite3.connect(temporary)
        source_connection.backup(target_connection, pages=256, sleep=0.05)
        target_connection.execute("PRAGMA journal_mode = DELETE")
        target_connection.close()
        target_connection = None
        result = verify_database(temporary)
        with temporary.open("r+b") as file:
            os.fsync(file.fileno())
        os.link(temporary, output)  # Exclusive, atomic publication; no overwrite race.
        result["path"] = str(output)
        return result
    finally:
        source_connection.close()
        if target_connection is not None:
            target_connection.close()
        if temporary.exists():
            temporary.unlink()


def _project_path(value: str) -> Path:
    path = Path(value).expanduser()
    return (path if path.is_absolute() else PROJECT_ROOT / path).resolve()


async def _initialize(path: Path) -> None:
    storage = SQLiteStorage(path)
    try:
        await storage.acquire_runtime()
        await storage.open()
    finally:
        await storage.close()


def _reset_transaction(connection) -> dict[str, int]:
    # Fixed table/column lists. Keep account/control state and admin history
    # byte-for-byte; never classify legacy sanctions by reason text or actor ID.
    protected_tables = ("users", "user_settings", "user_access", "trusted_applications",
                        "user_limits", "admin_audit_log", "schema_migrations")
    protected = {table: [tuple(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY 1")]
                 for table in protected_tables}
    counts = {}
    for table, condition in (
        ("notification_outbox", "appeal_id IS NOT NULL"),
        ("moderation_appeals", "1"),
        ("moderation_sanctions", "source = 'automatic_moderation'"),
        ("user_restrictions", "source = 'automatic_moderation'"),
        ("user_warnings", "1"),
        ("moderation_events", "1"),
        ("usage_attempts", "1"),
        ("usage_requests", "1"),
        ("activation_attempts", "1"),
    ):
        counts[table] = connection.execute(f"DELETE FROM {table} WHERE {condition}").rowcount
    for table, before in protected.items():
        if before != [tuple(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY 1")]:
            raise RuntimeError("Reset attempted to change protected account/control state")
    if [tuple(row) for row in connection.execute("PRAGMA integrity_check")] != [("ok",)] or connection.execute("PRAGMA foreign_key_check").fetchone():
        raise RuntimeError("Reset integrity check failed")
    return counts


async def reset_operational_state(source: Path, backup: Path) -> dict[str, int]:
    """Explicit local reset, never part of startup; refuse live bots or paths outside this project."""
    source, backup = source.resolve(), backup.resolve()
    if not source.is_relative_to(PROJECT_ROOT) or not backup.is_relative_to(PROJECT_ROOT):
        raise ValueError("Operational reset is restricted to this project")
    storage = SQLiteStorage(source)
    try:
        await storage.acquire_runtime()
        await asyncio.to_thread(verify_database, source)
        await asyncio.to_thread(backup_database, source, backup)
        await storage.open()
        counts = await storage.database.run(_reset_transaction, write=True)
        await asyncio.to_thread(verify_database, source)
        return counts
    finally:
        await storage.close()


def main() -> None:
    load_dotenv(PROJECT_ROOT / ".env")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("init", "backup", "verify", "restore", "reset-test-state"))
    parser.add_argument("--source", help="Defaults to SQLITE_PATH; required for restore")
    parser.add_argument("--output", help="New path; existing files are never overwritten")
    args = parser.parse_args()
    source = _project_path(args.source) if args.source else database_path()
    if args.command == "init":
        asyncio.run(_initialize(source))
        print(verify_database(source))
    elif args.command == "verify":
        print(verify_database(source))
    elif args.command == "reset-test-state":
        if not args.output:
            parser.error("Provide a new --output backup path before reset")
        # Counts only: never emit rows, explanations, profiles or configuration.
        print(json.dumps(asyncio.run(reset_operational_state(source, _project_path(args.output))), sort_keys=True))
    else:
        if not args.output or (args.command == "restore" and not args.source):
            parser.error("Provide --output and an explicit --source for restore")
        output = _project_path(args.output)
        if args.command == "restore":
            verify_database(source)
        print(backup_database(source, output))


if __name__ == "__main__":
    main()
