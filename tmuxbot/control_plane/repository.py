from __future__ import annotations

import errno
import json
import logging
import os
import sqlite3
import stat
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from tmuxbot.control_plane.migrations import MIGRATIONS
from tmuxbot.control_plane.models import RunEvent

log = logging.getLogger(__name__)


class ControlPlaneRepository:
    def __init__(self, path: Path):
        self.path = Path(os.path.abspath(path))

    def _connect(self) -> sqlite3.Connection:
        descriptor, expected_identity = self._prepare_storage()
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(self.path)
            _verify_database_identity(self.path, expected_identity)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout = 5000")
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA journal_mode = WAL")
            self._secure_storage_permissions()
        except BaseException as primary:
            if connection is not None:
                _cleanup_preserving_primary(connection.close, "close SQLite connection", primary)
            _cleanup_preserving_primary(
                lambda: os.close(descriptor), "close SQLite preflight descriptor", primary
            )
            raise
        try:
            os.close(descriptor)
        except BaseException as primary:
            _cleanup_preserving_primary(connection.close, "close SQLite connection", primary)
            raise
        return connection

    def _prepare_storage(self) -> tuple[int, tuple[int, int]]:
        _reject_symlink_ancestors(self.path.parent)
        try:
            parent_info = self.path.parent.lstat()
        except FileNotFoundError:
            self.path.parent.mkdir(parents=True, mode=0o700)
        else:
            if stat.S_ISLNK(parent_info.st_mode):
                raise RuntimeError(
                    f"SQLite data directory must not be a symbolic link: {self.path.parent}"
                )
        _secure_data_directory(self.path.parent)
        opened = _open_secure_regular_file(self.path, create=True)
        assert opened is not None
        descriptor, descriptor_info = opened
        try:
            for sidecar in self._sidecars():
                _secure_regular_file(sidecar, missing_ok=True)
        except BaseException as primary:
            _cleanup_preserving_primary(
                lambda: os.close(descriptor), "close SQLite preflight descriptor", primary
            )
            raise
        return descriptor, (descriptor_info.st_dev, descriptor_info.st_ino)

    def _secure_storage_permissions(self) -> None:
        _secure_data_directory(self.path.parent)
        _secure_regular_file(self.path)
        for sidecar in self._sidecars():
            _secure_regular_file(sidecar, missing_ok=True)

    def _sidecars(self) -> tuple[Path, Path]:
        return (
            self.path.with_name(f"{self.path.name}-wal"),
            self.path.with_name(f"{self.path.name}-shm"),
        )

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        primary: BaseException | None = None
        try:
            try:
                yield connection
            except BaseException as exc:
                primary = exc
                _cleanup_preserving_primary(connection.rollback, "rollback SQLite transaction", exc)
                raise
            try:
                self._secure_storage_permissions()
            except BaseException as exc:
                primary = exc
                _cleanup_preserving_primary(connection.rollback, "rollback SQLite transaction", exc)
                raise
            try:
                connection.commit()
            except BaseException as exc:
                primary = exc
                _cleanup_preserving_primary(connection.rollback, "rollback SQLite transaction", exc)
                raise
        finally:
            if primary is None:
                connection.close()
            else:
                _cleanup_preserving_primary(connection.close, "close SQLite connection", primary)

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


def _secure_data_directory(path: Path) -> None:
    _reject_symlink_ancestors(path)
    try:
        path_info = path.lstat()
    except OSError as exc:
        raise RuntimeError(f"unable to inspect SQLite data directory {path}: {exc}") from exc
    if stat.S_ISLNK(path_info.st_mode):
        raise RuntimeError(f"SQLite data directory must not be a symbolic link: {path}")

    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise RuntimeError(f"unable to open private SQLite data directory {path}: {exc}") from exc
    try:
        descriptor_info = os.fstat(descriptor)
        if not stat.S_ISDIR(descriptor_info.st_mode):
            raise RuntimeError(f"SQLite data directory is not a directory: {path}")
        _set_descriptor_mode(descriptor, path, 0o700)
    finally:
        os.close(descriptor)


def _secure_regular_file(
    path: Path, *, create: bool = False, missing_ok: bool = False
) -> None:
    opened = _open_secure_regular_file(path, create=create, missing_ok=missing_ok)
    if opened is None:
        return
    descriptor, _descriptor_info = opened
    os.close(descriptor)


def _open_secure_regular_file(
    path: Path, *, create: bool = False, missing_ok: bool = False
) -> tuple[int, os.stat_result] | None:
    flags = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW
    if create:
        flags = os.O_RDWR | os.O_CREAT | os.O_NONBLOCK | os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileNotFoundError:
        if missing_ok:
            return
        raise RuntimeError(f"SQLite storage file disappeared while securing permissions: {path}")
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise RuntimeError(f"refusing symbolic link for SQLite storage file: {path}") from exc
        if exc.errno == errno.EISDIR:
            raise RuntimeError(f"SQLite storage path is not a regular file: {path}") from exc
        raise RuntimeError(f"unable to open SQLite storage file securely {path}: {exc}") from exc
    try:
        descriptor_info = os.fstat(descriptor)
        if not stat.S_ISREG(descriptor_info.st_mode):
            raise RuntimeError(f"SQLite storage path is not a regular file: {path}")
        _set_descriptor_mode(descriptor, path, 0o600)
        return descriptor, os.fstat(descriptor)
    except BaseException as primary:
        _cleanup_preserving_primary(
            lambda: os.close(descriptor), "close SQLite storage descriptor", primary
        )
        raise


def _reject_symlink_ancestors(path: Path) -> None:
    # This catches static symlinks and detectable path replacement. An actively
    # malicious same-UID process is outside the project's local threat model.
    absolute_path = Path(os.path.abspath(path))
    for ancestor in reversed(absolute_path.parents):
        try:
            ancestor_info = ancestor.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise RuntimeError(f"unable to inspect SQLite path ancestor {ancestor}: {exc}") from exc
        if stat.S_ISLNK(ancestor_info.st_mode):
            raise RuntimeError(f"SQLite path ancestor must not be a symbolic link: {ancestor}")


def _verify_database_identity(path: Path, expected_identity: tuple[int, int]) -> None:
    try:
        current_info = os.stat(path, follow_symlinks=False)
    except OSError as exc:
        raise RuntimeError(f"unable to verify SQLite database path identity {path}: {exc}") from exc
    current_identity = (current_info.st_dev, current_info.st_ino)
    if not stat.S_ISREG(current_info.st_mode) or current_identity != expected_identity:
        raise RuntimeError(f"SQLite database path changed before SQLite connected: {path}")


def _cleanup_preserving_primary(
    cleanup: Callable[[], object], description: str, primary: BaseException
) -> None:
    try:
        cleanup()
    except BaseException:
        log.warning(
            "%s failed while preserving primary %s",
            description,
            type(primary).__name__,
            exc_info=True,
        )


def _set_descriptor_mode(descriptor: int, path: Path, mode: int) -> None:
    try:
        os.fchmod(descriptor, mode)
        actual_mode = stat.S_IMODE(os.fstat(descriptor).st_mode)
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
