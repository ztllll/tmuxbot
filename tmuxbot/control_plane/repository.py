from __future__ import annotations

import json
import os
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from tmuxbot.control_plane.migrations import MIGRATIONS
from tmuxbot.control_plane.models import RunEvent


class ControlPlaneRepository:
    def __init__(self, path: Path):
        self.path = path

    def _connect(self) -> sqlite3.Connection:
        self._prepare_storage()
        connection = sqlite3.connect(self.path)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout = 5000")
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA journal_mode = WAL")
            self._secure_storage_permissions()
            return connection
        except BaseException:
            connection.close()
            raise

    def _prepare_storage(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        _set_private_mode(self.path.parent, 0o700)
        try:
            descriptor = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        except OSError as exc:
            raise RuntimeError(f"unable to prepare private SQLite database {self.path}: {exc}") from exc
        else:
            os.close(descriptor)
        self._secure_storage_permissions()

    def _secure_storage_permissions(self) -> None:
        _set_private_mode(self.path.parent, 0o700)
        for path in (
            self.path,
            self.path.with_name(f"{self.path.name}-wal"),
            self.path.with_name(f"{self.path.name}-shm"),
        ):
            if path.exists():
                _set_private_mode(path, 0o600)

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            try:
                self._secure_storage_permissions()
            finally:
                connection.close()
                self._secure_storage_permissions()

    def migrate(self) -> None:
        versions = [version for version, _sql in MIGRATIONS]
        if any(current <= previous for previous, current in zip(versions, versions[1:])):
            raise ValueError("migration versions must be strictly increasing without duplicates")
        maximum_supported = versions[-1] if versions else 0

        with self._connection() as db:
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

    def append_event(self, event: RunEvent) -> bool:
        with self._connection() as db:
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
        with self._connection() as db:
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
        with self._connection() as db:
            db.execute(
                "INSERT INTO settings(key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (key, value, int(time.time())),
            )

    def get_setting(self, key: str) -> str | None:
        with self._connection() as db:
            row = db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return None if row is None else str(row["value"])

    def set_setting_if_absent(self, key: str, value: str) -> bool:
        with self._connection() as db:
            cursor = db.execute(
                "INSERT INTO settings(key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO NOTHING",
                (key, value, int(time.time())),
            )
            return cursor.rowcount == 1

    def create_session(self, token_hash: str, csrf_token: str, *, expires_at: int) -> None:
        with self._connection() as db:
            now = int(time.time())
            db.execute("DELETE FROM web_sessions WHERE expires_at <= ?", (now,))
            db.execute(
                "INSERT INTO web_sessions(token_hash, csrf_token, expires_at, created_at) "
                "VALUES (?, ?, ?, ?)",
                (token_hash, csrf_token, expires_at, now),
            )

    def get_session(self, token_hash: str, *, now: int) -> str | None:
        with self._connection() as db:
            row = db.execute(
                "SELECT csrf_token FROM web_sessions "
                "WHERE token_hash = ? AND expires_at > ?",
                (token_hash, now),
            ).fetchone()
        return None if row is None else str(row["csrf_token"])

    def delete_session(self, token_hash: str) -> None:
        with self._connection() as db:
            db.execute("DELETE FROM web_sessions WHERE token_hash = ?", (token_hash,))


def _set_private_mode(path: Path, mode: int) -> None:
    try:
        path.chmod(mode)
        actual_mode = path.stat().st_mode & 0o777
    except OSError as exc:
        raise RuntimeError(
            f"unable to secure SQLite storage permissions for {path}: {exc}"
        ) from exc
    if actual_mode != mode:
        raise RuntimeError(
            f"unable to secure SQLite storage permissions for {path}: "
            f"expected {mode:#o}, found {actual_mode:#o}"
        )


def _migration_statements(sql: str) -> Iterator[str]:
    buffer: list[str] = []
    for character in sql:
        buffer.append(character)
        if character == ";" and sqlite3.complete_statement("".join(buffer)):
            statement = "".join(buffer).strip()
            if statement:
                yield statement
            buffer.clear()
    remainder = "".join(buffer).strip()
    if remainder and not _is_sql_comment_only(remainder):
        raise ValueError("migration SQL must contain complete semicolon-terminated statements")


def _is_sql_comment_only(sql: str) -> bool:
    remainder = sql.lstrip()
    while remainder:
        if remainder.startswith("--"):
            newline = remainder.find("\n")
            if newline == -1:
                return True
            remainder = remainder[newline + 1 :].lstrip()
            continue
        if remainder.startswith("/*"):
            comment_end = remainder.find("*/", 2)
            if comment_end == -1:
                return False
            remainder = remainder[comment_end + 2 :].lstrip()
            continue
        return False
    return True
