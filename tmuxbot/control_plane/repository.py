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
from tmuxbot.control_plane.models import (
    ManagedSession,
    ProjectRecord,
    ProviderProfile,
    ProviderProbeResult,
    RunEvent,
)

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

    def upsert_provider_profile(self, profile: ProviderProfile) -> ProviderProfile:
        with self._connection() as db:
            existing = db.execute(
                "SELECT id FROM provider_profiles "
                "WHERE binary_name = ? AND executable_path = ?",
                (profile.binary_name, profile.executable_path),
            ).fetchone()
            provider_id = profile.id if existing is None else str(existing["id"])
            db.execute(
                "INSERT INTO provider_profiles "
                "(id, binary_name, executable_path, version, device, inode, mtime_ns, discovered_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(binary_name, executable_path) DO UPDATE SET "
                "version=CASE WHEN provider_profiles.device=excluded.device "
                "AND provider_profiles.inode=excluded.inode "
                "AND provider_profiles.mtime_ns=excluded.mtime_ns "
                "THEN COALESCE(excluded.version, provider_profiles.version) "
                "ELSE excluded.version END, "
                "device=excluded.device, inode=excluded.inode, "
                "mtime_ns=excluded.mtime_ns, discovered_at=excluded.discovered_at",
                (
                    provider_id,
                    profile.binary_name,
                    profile.executable_path,
                    profile.version,
                    profile.device,
                    profile.inode,
                    profile.mtime_ns,
                    profile.discovered_at,
                ),
            )
        stored = self.get_provider_profile(provider_id)
        assert stored is not None
        return stored

    def update_provider_version(self, provider_id: str, version: str | None) -> None:
        with self._connection() as db:
            db.execute(
                "UPDATE provider_profiles SET version = ? WHERE id = ?",
                (version, provider_id),
            )

    def get_provider_profile(self, provider_id: str) -> ProviderProfile | None:
        with self._connection() as db:
            row = db.execute(
                "SELECT * FROM provider_profiles WHERE id = ?", (provider_id,)
            ).fetchone()
        return None if row is None else _provider_profile(row)

    def list_provider_profiles(self) -> list[ProviderProfile]:
        with self._connection() as db:
            rows = db.execute(
                "SELECT * FROM provider_profiles ORDER BY binary_name, executable_path"
            ).fetchall()
        return [_provider_profile(row) for row in rows]

    def delete_provider_profile(self, provider_id: str) -> bool:
        with self._connection() as db:
            cursor = db.execute(
                "DELETE FROM provider_profiles WHERE id = ?", (provider_id,)
            )
            return cursor.rowcount == 1

    def create_project(self, project: ProjectRecord) -> None:
        with self._connection() as db:
            db.execute(
                "INSERT INTO projects "
                "(id, name, root_path, device, inode, mtime_ns, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    project.id,
                    project.name,
                    project.root_path,
                    project.device,
                    project.inode,
                    project.mtime_ns,
                    project.created_at,
                ),
            )

    def list_projects(self) -> list[ProjectRecord]:
        with self._connection() as db:
            rows = db.execute("SELECT * FROM projects ORDER BY created_at, id").fetchall()
        return [_project_record(row) for row in rows]

    def get_project(self, project_id: str) -> ProjectRecord | None:
        with self._connection() as db:
            row = db.execute(
                "SELECT * FROM projects WHERE id = ?", (project_id,)
            ).fetchone()
        return None if row is None else _project_record(row)

    def update_project(self, project: ProjectRecord) -> bool:
        with self._connection() as db:
            cursor = db.execute(
                "UPDATE projects SET name = ?, root_path = ?, device = ?, inode = ?, "
                "mtime_ns = ? WHERE id = ?",
                (
                    project.name,
                    project.root_path,
                    project.device,
                    project.inode,
                    project.mtime_ns,
                    project.id,
                ),
            )
            return cursor.rowcount == 1

    def delete_project(self, project_id: str) -> bool:
        with self._connection() as db:
            cursor = db.execute("DELETE FROM projects WHERE id = ?", (project_id,))
            return cursor.rowcount == 1

    def create_managed_session(self, session: ManagedSession) -> None:
        with self._connection() as db:
            db.execute(
                "INSERT INTO managed_sessions "
                "(id, project_id, provider_id, name, tmux_session, tmux_window, "
                "tmux_pane, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    session.id,
                    session.project_id,
                    session.provider_id,
                    session.name,
                    session.tmux_session,
                    session.tmux_window,
                    session.tmux_pane,
                    session.status,
                    session.created_at,
                ),
            )

    def list_managed_sessions(self) -> list[ManagedSession]:
        with self._connection() as db:
            rows = db.execute(
                "SELECT * FROM managed_sessions ORDER BY created_at, id"
            ).fetchall()
        return [_managed_session(row) for row in rows]

    def get_managed_session(self, session_id: str) -> ManagedSession | None:
        with self._connection() as db:
            row = db.execute(
                "SELECT * FROM managed_sessions WHERE id = ?", (session_id,)
            ).fetchone()
        return None if row is None else _managed_session(row)

    def update_managed_session(self, session: ManagedSession) -> bool:
        with self._connection() as db:
            cursor = db.execute(
                "UPDATE managed_sessions SET project_id = ?, provider_id = ?, name = ?, "
                "tmux_session = ?, tmux_window = ?, tmux_pane = ?, status = ? WHERE id = ?",
                (
                    session.project_id,
                    session.provider_id,
                    session.name,
                    session.tmux_session,
                    session.tmux_window,
                    session.tmux_pane,
                    session.status,
                    session.id,
                ),
            )
            return cursor.rowcount == 1

    def delete_managed_session(self, session_id: str) -> bool:
        with self._connection() as db:
            cursor = db.execute(
                "DELETE FROM managed_sessions WHERE id = ?", (session_id,)
            )
            return cursor.rowcount == 1

    def record_probe_result(self, result: ProviderProbeResult) -> None:
        with self._connection() as db:
            db.execute(
                "INSERT INTO probe_results "
                "(id, provider_id, success, version, error_code, exit_code, duration_ms, "
                "output_truncated, observed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    result.id,
                    result.provider_id,
                    int(result.success),
                    result.version,
                    result.error_code,
                    result.exit_code,
                    result.duration_ms,
                    int(result.output_truncated),
                    result.observed_at,
                ),
            )

    def list_probe_results(self, provider_id: str) -> list[ProviderProbeResult]:
        with self._connection() as db:
            rows = db.execute(
                "SELECT * FROM probe_results WHERE provider_id = ? "
                "ORDER BY observed_at, id",
                (provider_id,),
            ).fetchall()
        return [
            ProviderProbeResult(
                id=row["id"],
                provider_id=row["provider_id"],
                success=bool(row["success"]),
                version=row["version"],
                error_code=row["error_code"],
                exit_code=row["exit_code"],
                duration_ms=row["duration_ms"],
                output_truncated=bool(row["output_truncated"]),
                observed_at=row["observed_at"],
            )
            for row in rows
        ]


def _provider_profile(row: sqlite3.Row) -> ProviderProfile:
    return ProviderProfile(
        id=row["id"],
        binary_name=row["binary_name"],
        executable_path=row["executable_path"],
        version=row["version"],
        device=row["device"],
        inode=row["inode"],
        mtime_ns=row["mtime_ns"],
        discovered_at=row["discovered_at"],
    )


def _project_record(row: sqlite3.Row) -> ProjectRecord:
    return ProjectRecord(
        id=row["id"],
        name=row["name"],
        root_path=row["root_path"],
        device=row["device"],
        inode=row["inode"],
        mtime_ns=row["mtime_ns"],
        created_at=row["created_at"],
    )


def _managed_session(row: sqlite3.Row) -> ManagedSession:
    return ManagedSession(
        id=row["id"],
        project_id=row["project_id"],
        provider_id=row["provider_id"],
        name=row["name"],
        tmux_session=row["tmux_session"],
        tmux_window=row["tmux_window"],
        tmux_pane=row["tmux_pane"],
        status=row["status"],
        created_at=row["created_at"],
    )


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
