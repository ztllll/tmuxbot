from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

from tmuxbot.control_plane.migrations import MIGRATIONS
from tmuxbot.control_plane.models import RunEvent


class ControlPlaneRepository:
    def __init__(self, path: Path):
        self.path = path

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def migrate(self) -> None:
        versions = [version for version, _sql in MIGRATIONS]
        if any(current <= previous for previous, current in zip(versions, versions[1:])):
            raise ValueError("migration versions must be strictly increasing without duplicates")
        maximum_supported = versions[-1] if versions else 0

        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations "
                "(version INTEGER PRIMARY KEY, applied_at INTEGER NOT NULL)"
            )
            applied = {row[0] for row in db.execute("SELECT version FROM schema_migrations")}
            newer_versions = [version for version in applied if version > maximum_supported]
            if newer_versions:
                raise RuntimeError(
                    "database schema is newer than supported: "
                    f"found version {max(newer_versions)}, maximum supported is "
                    f"{maximum_supported}"
                )
            for version, sql in MIGRATIONS:
                if version in applied:
                    continue
                for statement in _migration_statements(sql):
                    db.execute(statement)
                db.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (version, int(time.time())),
                )
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def append_event(self, event: RunEvent) -> bool:
        with self._connect() as db:
            cursor = db.execute(
                "INSERT INTO run_events "
                "(event_id, event_type, aggregate_type, aggregate_id, payload_json, occurred_at) "
                "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(event_id) DO NOTHING",
                (
                    event.event_id,
                    event.event_type,
                    event.aggregate_type,
                    event.aggregate_id,
                    json.dumps(dict(event.payload), ensure_ascii=False, sort_keys=True),
                    event.occurred_at.isoformat(),
                ),
            )
            return cursor.rowcount == 1

    def list_events(self, *, after_sequence: int, limit: int) -> list[RunEvent]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM run_events WHERE sequence > ? ORDER BY sequence LIMIT ?",
                (after_sequence, min(max(limit, 1), 500)),
            ).fetchall()
        return [
            RunEvent(
                event_id=row["event_id"],
                event_type=row["event_type"],
                aggregate_type=row["aggregate_type"],
                aggregate_id=row["aggregate_id"],
                payload=json.loads(row["payload_json"]),
                occurred_at=datetime.fromisoformat(row["occurred_at"]),
                sequence=row["sequence"],
            )
            for row in rows
        ]

    def set_setting(self, key: str, value: str) -> None:
        with self._connect() as db:
            db.execute(
                "INSERT INTO settings(key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (key, value, int(time.time())),
            )

    def get_setting(self, key: str) -> str | None:
        with self._connect() as db:
            row = db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return None if row is None else str(row["value"])

    def set_setting_if_absent(self, key: str, value: str) -> bool:
        with self._connect() as db:
            cursor = db.execute(
                "INSERT INTO settings(key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO NOTHING",
                (key, value, int(time.time())),
            )
            return cursor.rowcount == 1

    def create_session(self, token_hash: str, csrf_token: str, *, expires_at: int) -> None:
        with self._connect() as db:
            now = int(time.time())
            db.execute("DELETE FROM web_sessions WHERE expires_at <= ?", (now,))
            db.execute(
                "INSERT INTO web_sessions(token_hash, csrf_token, expires_at, created_at) "
                "VALUES (?, ?, ?, ?)",
                (token_hash, csrf_token, expires_at, now),
            )

    def get_session(self, token_hash: str, *, now: int) -> str | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT csrf_token FROM web_sessions "
                "WHERE token_hash = ? AND expires_at > ?",
                (token_hash, now),
            ).fetchone()
        return None if row is None else str(row["csrf_token"])

    def delete_session(self, token_hash: str) -> None:
        with self._connect() as db:
            db.execute("DELETE FROM web_sessions WHERE token_hash = ?", (token_hash,))


def _migration_statements(sql: str) -> Iterator[str]:
    buffer: list[str] = []
    for character in sql:
        buffer.append(character)
        if character == ";" and sqlite3.complete_statement("".join(buffer)):
            statement = "".join(buffer).strip()
            if statement:
                yield statement
            buffer.clear()
    if "".join(buffer).strip():
        raise ValueError("migration SQL must contain complete semicolon-terminated statements")
