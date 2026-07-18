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
from tmuxbot.teamrun.domain import (
    AgentRole,
    MailboxMessage,
    TeamArtifact,
    DispatchCommand,
    TeamAgent,
    TeamRun,
    TeamRunSnapshot,
    TeamRunState,
    TeamTask,
    TeamTaskState,
    TaskWorktreeRecord,
    WriteLease,
    validate_task_graph,
)
from tmuxbot.teamrun.protocol import TaskAssignment

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

    def has_event(self, event_id: str) -> bool:
        with self._connection() as db:
            return (
                db.execute("SELECT 1 FROM run_events WHERE event_id = ?", (event_id,)).fetchone()
                is not None
            )

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

    def list_teamrun_events(
        self, run_id: str, *, after_sequence: int, limit: int
    ) -> list[RunEvent]:
        with self._connection() as db:
            rows = db.execute(
                "SELECT * FROM run_events WHERE sequence > ? AND ("
                "(aggregate_type = 'team_run' AND aggregate_id = ?) "
                "OR json_extract(payload_json, '$.run_id') = ?) "
                "ORDER BY sequence LIMIT ?",
                (after_sequence, run_id, run_id, min(max(limit, 1), 500)),
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
                "SELECT csrf_token FROM web_sessions WHERE token_hash = ? AND expires_at > ?",
                (token_hash, now),
            ).fetchone()
        return None if row is None else str(row["csrf_token"])

    def delete_session(self, token_hash: str) -> None:
        with self._connection() as db:
            db.execute("DELETE FROM web_sessions WHERE token_hash = ?", (token_hash,))

    def create_team_run(
        self,
        run: TeamRun,
        agents: list[TeamAgent],
        tasks: list[TeamTask],
        *,
        event_id: str,
    ) -> bool:
        validate_task_graph(tasks)
        if any(agent.run_id != run.run_id for agent in agents):
            raise ValueError("all agents must belong to the run")
        if any(task.run_id != run.run_id for task in tasks):
            raise ValueError("all tasks must belong to the run")
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM run_events WHERE event_id = ?", (event_id,)).fetchone():
                return False
            db.execute(
                "INSERT INTO team_runs(run_id, goal, state, max_retries, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    run.run_id,
                    run.goal,
                    run.state.value,
                    run.max_retries,
                    run.created_at.isoformat(),
                    run.updated_at.isoformat(),
                ),
            )
            for agent in agents:
                db.execute(
                    "INSERT INTO team_agents(agent_id, run_id, role, managed_session_id) "
                    "VALUES (?, ?, ?, ?)",
                    (agent.agent_id, agent.run_id, agent.role.value, agent.managed_session_id),
                )
            for task in tasks:
                db.execute(
                    "INSERT INTO team_tasks("
                    "task_id, run_id, title, goal, role, state, dependencies_json, "
                    "requires_write, max_attempts, attempt, assignee_agent_id, "
                    "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        task.task_id,
                        task.run_id,
                        task.title,
                        task.goal,
                        task.role.value,
                        task.state.value,
                        json.dumps(list(task.dependencies)),
                        int(task.requires_write),
                        task.max_attempts,
                        task.attempt,
                        task.assignee_agent_id,
                        task.created_at.isoformat(),
                        task.updated_at.isoformat(),
                    ),
                )
            _append_event_db(
                db,
                RunEvent(
                    event_id=event_id,
                    event_type="teamrun.created",
                    aggregate_type="team_run",
                    aggregate_id=run.run_id,
                    payload={"goal": run.goal, "task_count": len(tasks)},
                    occurred_at=run.created_at,
                ),
            )
            return True

    def get_team_run(self, run_id: str) -> TeamRunSnapshot:
        with self._connection() as db:
            run_row = db.execute("SELECT * FROM team_runs WHERE run_id = ?", (run_id,)).fetchone()
            if run_row is None:
                raise KeyError(run_id)
            agent_rows = db.execute(
                "SELECT * FROM team_agents WHERE run_id = ? "
                "ORDER BY CASE role WHEN 'coordinator' THEN 1 WHEN 'implementer' THEN 2 ELSE 3 END",
                (run_id,),
            ).fetchall()
            task_rows = db.execute(
                "SELECT * FROM team_tasks WHERE run_id = ? ORDER BY created_at, rowid",
                (run_id,),
            ).fetchall()
        return TeamRunSnapshot(
            run=_run_from_row(run_row),
            agents=tuple(_agent_from_row(row) for row in agent_rows),
            tasks=tuple(_task_from_row(row) for row in task_rows),
        )

    def acquire_write_lease(
        self, lease_id: str, run_id: str, task_id: str, *, now: datetime
    ) -> bool:
        with self._connection() as db:
            try:
                db.execute(
                    "INSERT INTO write_leases(lease_id, run_id, task_id, acquired_at) "
                    "VALUES (?, ?, ?, ?)",
                    (lease_id, run_id, task_id, now.isoformat()),
                )
            except sqlite3.IntegrityError as exc:
                if "write_leases.run_id" in str(exc):
                    return False
                raise
            return True

    def release_write_lease(self, run_id: str, task_id: str, *, now: datetime) -> None:
        with self._connection() as db:
            db.execute(
                "UPDATE write_leases SET released_at = ? "
                "WHERE run_id = ? AND task_id = ? AND released_at IS NULL",
                (now.isoformat(), run_id, task_id),
            )

    def get_active_write_lease(self, run_id: str) -> WriteLease | None:
        with self._connection() as db:
            row = db.execute(
                "SELECT * FROM write_leases WHERE run_id = ? AND released_at IS NULL",
                (run_id,),
            ).fetchone()
        return None if row is None else _lease_from_row(row)

    def list_mailbox(self, run_id: str) -> list[MailboxMessage]:
        with self._connection() as db:
            rows = db.execute(
                "SELECT * FROM mailbox_messages WHERE run_id = ? ORDER BY created_at, rowid",
                (run_id,),
            ).fetchall()
        return [_message_from_row(row) for row in rows]

    def mark_mailbox_delivered(self, run_id: str, idempotency_key: str, *, now: datetime) -> None:
        with self._connection() as db:
            db.execute(
                "UPDATE mailbox_messages SET delivered_at = COALESCE(delivered_at, ?) "
                "WHERE run_id = ? AND idempotency_key = ?",
                (now.isoformat(), run_id, idempotency_key),
            )

    def list_dispatch_commands(
        self, run_id: str, *, states: set[str] | None = None
    ) -> list[DispatchCommand]:
        query = "SELECT * FROM dispatch_commands WHERE run_id = ?"
        params: list[object] = [run_id]
        if states:
            query += " AND state IN (" + ", ".join("?" for _ in states) + ")"
            params.extend(sorted(states))
        query += " ORDER BY created_at, command_id"
        with self._connection() as db:
            rows = db.execute(query, params).fetchall()
        return [_dispatch_command_from_row(row) for row in rows]

    def create_task_worktree(self, record: TaskWorktreeRecord) -> bool:
        with self._connection() as db:
            cursor = db.execute(
                "INSERT INTO task_worktrees(run_id, task_id, attempt, path, branch, "
                "managed_session_id, state, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(run_id, task_id, attempt) DO NOTHING",
                (
                    record.run_id,
                    record.task_id,
                    record.attempt,
                    record.path,
                    record.branch,
                    record.managed_session_id,
                    record.state,
                    record.created_at.isoformat(),
                ),
            )
            return cursor.rowcount == 1

    def list_task_worktrees(self, run_id: str) -> list[TaskWorktreeRecord]:
        with self._connection() as db:
            rows = db.execute(
                "SELECT * FROM task_worktrees WHERE run_id = ? ORDER BY created_at, task_id, attempt",
                (run_id,),
            ).fetchall()
        return [_task_worktree_from_row(row) for row in rows]

    def release_task_worktree(
        self, run_id: str, task_id: str, attempt: int, *, now: datetime
    ) -> bool:
        with self._connection() as db:
            cursor = db.execute(
                "UPDATE task_worktrees SET state = 'released', released_at = ? WHERE run_id = ? "
                "AND task_id = ? AND attempt = ? AND state = 'active'",
                (now.isoformat(), run_id, task_id, attempt),
            )
            return cursor.rowcount == 1

    def mark_dispatch_tmux_written(self, command_id: str, *, now: datetime) -> DispatchCommand:
        with self._connection() as db:
            db.execute(
                "UPDATE dispatch_commands SET state = 'tmux_written', "
                "tmux_written_at = COALESCE(tmux_written_at, ?) WHERE command_id = ? "
                "AND state = 'pending'",
                (now.isoformat(), command_id),
            )
            row = db.execute(
                "SELECT * FROM dispatch_commands WHERE command_id = ?", (command_id,)
            ).fetchone()
        if row is None:
            raise KeyError(command_id)
        return _dispatch_command_from_row(row)

    def mark_dispatch_uncertain(
        self, command_id: str, *, error: str, now: datetime
    ) -> DispatchCommand:
        with self._connection() as db:
            db.execute(
                "UPDATE dispatch_commands SET state = 'uncertain', last_error = ?, "
                "tmux_written_at = COALESCE(tmux_written_at, ?) WHERE command_id = ? "
                "AND state = 'pending'",
                (error[:500], now.isoformat(), command_id),
            )
            row = db.execute(
                "SELECT * FROM dispatch_commands WHERE command_id = ?", (command_id,)
            ).fetchone()
        if row is None:
            raise KeyError(command_id)
        return _dispatch_command_from_row(row)

    def list_artifacts(self, run_id: str, task_id: str | None = None) -> list[TeamArtifact]:
        query = "SELECT * FROM artifacts WHERE run_id = ?"
        params: tuple[object, ...] = (run_id,)
        if task_id is not None:
            query += " AND task_id = ?"
            params += (task_id,)
        query += " ORDER BY created_at, rowid"
        with self._connection() as db:
            rows = db.execute(query, params).fetchall()
        return [_artifact_from_row(row) for row in rows]

    def set_team_run_state(
        self,
        run_id: str,
        *,
        allowed: set[TeamRunState],
        state: TeamRunState,
        event_id: str,
        now: datetime,
        payload: dict[str, object] | None = None,
    ) -> TeamRunState:
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT state FROM team_runs WHERE run_id = ?", (run_id,)).fetchone()
            if row is None:
                raise KeyError(run_id)
            if db.execute("SELECT 1 FROM run_events WHERE event_id = ?", (event_id,)).fetchone():
                return TeamRunState(row["state"])
            current = TeamRunState(row["state"])
            if current not in allowed:
                raise ValueError(f"cannot transition run from {current.value} to {state.value}")
            db.execute(
                "UPDATE team_runs SET state = ?, updated_at = ? WHERE run_id = ?",
                (state.value, now.isoformat(), run_id),
            )
            _append_event_db(
                db,
                RunEvent(
                    event_id=event_id,
                    event_type=f"teamrun.{state.value}",
                    aggregate_type="team_run",
                    aggregate_id=run_id,
                    payload=payload or {},
                    occurred_at=now,
                ),
            )
            return state

    def refresh_task_readiness(self, run_id: str, *, now: datetime) -> list[TeamTask]:
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute(
                "SELECT * FROM team_tasks WHERE run_id = ? ORDER BY created_at, rowid",
                (run_id,),
            ).fetchall()
            accepted = {
                row["task_id"] for row in rows if row["state"] == TeamTaskState.ACCEPTED.value
            }
            changed: list[TeamTask] = []
            for row in rows:
                if row["state"] not in {
                    TeamTaskState.PENDING.value,
                    TeamTaskState.RETRYING.value,
                }:
                    continue
                dependencies = tuple(json.loads(row["dependencies_json"]))
                if not set(dependencies).issubset(accepted):
                    continue
                db.execute(
                    "UPDATE team_tasks SET state = ?, updated_at = ? "
                    "WHERE run_id = ? AND task_id = ?",
                    (
                        TeamTaskState.READY.value,
                        now.isoformat(),
                        run_id,
                        row["task_id"],
                    ),
                )
                event_id = f"teamrun:{run_id}:task:{row['task_id']}:ready:{row['attempt']}"
                _append_event_db(
                    db,
                    RunEvent(
                        event_id=event_id,
                        event_type="teamtask.ready",
                        aggregate_type="team_task",
                        aggregate_id=row["task_id"],
                        payload={"run_id": run_id, "dependencies": list(dependencies)},
                        occurred_at=now,
                    ),
                )
                changed_row = db.execute(
                    "SELECT * FROM team_tasks WHERE run_id = ? AND task_id = ?",
                    (run_id, row["task_id"]),
                ).fetchone()
                changed.append(_task_from_row(changed_row))
            return changed

    def list_ready_tasks(self, run_id: str) -> list[TeamTask]:
        with self._connection() as db:
            rows = db.execute(
                "SELECT * FROM team_tasks WHERE run_id = ? AND state = ? "
                "ORDER BY created_at, rowid",
                (run_id, TeamTaskState.READY.value),
            ).fetchall()
        return [_task_from_row(row) for row in rows]

    def get_team_task(self, run_id: str, task_id: str) -> TeamTask:
        with self._connection() as db:
            row = db.execute(
                "SELECT * FROM team_tasks WHERE run_id = ? AND task_id = ?",
                (run_id, task_id),
            ).fetchone()
        if row is None:
            raise KeyError(task_id)
        return _task_from_row(row)

    def claim_team_task(
        self,
        run_id: str,
        task_id: str,
        *,
        event_id: str,
        dispatch_session_id: str | None = None,
        now: datetime,
    ) -> tuple[TeamTask, TeamAgent] | None:
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            run_row = db.execute(
                "SELECT state FROM team_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if run_row is None:
                raise KeyError(run_id)
            if run_row["state"] != TeamRunState.RUNNING.value:
                return None
            task_row = db.execute(
                "SELECT * FROM team_tasks WHERE run_id = ? AND task_id = ?",
                (run_id, task_id),
            ).fetchone()
            if task_row is None:
                raise KeyError(task_id)
            if task_row["state"] != TeamTaskState.READY.value:
                return None
            agent_row = db.execute(
                "SELECT * FROM team_agents WHERE run_id = ? AND role = ?",
                (run_id, task_row["role"]),
            ).fetchone()
            if agent_row is None:
                raise ValueError(f"run has no agent for role {task_row['role']!r}")
            next_attempt = int(task_row["attempt"]) + 1
            if task_row["requires_write"]:
                try:
                    db.execute(
                        "INSERT INTO write_leases(lease_id, run_id, task_id, acquired_at) "
                        "VALUES (?, ?, ?, ?)",
                        (
                            f"lease:{run_id}:{task_id}:{next_attempt}",
                            run_id,
                            task_id,
                            now.isoformat(),
                        ),
                    )
                except sqlite3.IntegrityError as exc:
                    if "write_leases.run_id" in str(exc):
                        return None
                    raise
            db.execute(
                "UPDATE team_tasks SET state = ?, assignee_agent_id = ?, attempt = ?, "
                "updated_at = ? WHERE run_id = ? AND task_id = ?",
                (
                    TeamTaskState.ASSIGNED.value,
                    agent_row["agent_id"],
                    next_attempt,
                    now.isoformat(),
                    run_id,
                    task_id,
                ),
            )
            dependencies = json.loads(task_row["dependencies_json"])
            dispatch_key = f"teamrun:{run_id}:dispatch:{task_id}:{next_attempt}"
            envelope = TaskAssignment(
                message_id=dispatch_key,
                run_id=run_id,
                task_id=task_id,
                attempt=next_attempt,
                assignee_agent_id=agent_row["agent_id"],
                role=AgentRole(task_row["role"]),
                goal=task_row["goal"],
                constraints=(
                    "shared-directory single writer",
                    "publish evidence before review",
                ),
                dependencies=tuple(dependencies),
                expected_artifacts=("evidence",),
                acceptance_criteria=("publish evidence before review",),
                idempotency_key=dispatch_key,
            ).to_wire()
            db.execute(
                "INSERT INTO mailbox_messages("
                "message_id, run_id, task_id, sender_agent_id, recipient_agent_id, kind, "
                "body_json, idempotency_key, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    f"message:{event_id}",
                    run_id,
                    task_id,
                    None,
                    agent_row["agent_id"],
                    "task_dispatch",
                    json.dumps(envelope, ensure_ascii=False, sort_keys=True),
                    dispatch_key,
                    now.isoformat(),
                ),
            )
            db.execute(
                "INSERT INTO dispatch_commands(command_id, run_id, task_id, attempt, "
                "managed_session_id, envelope_json, state, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)",
                (
                    dispatch_key,
                    run_id,
                    task_id,
                    next_attempt,
                    dispatch_session_id or agent_row["managed_session_id"],
                    json.dumps(envelope, ensure_ascii=False, sort_keys=True),
                    now.isoformat(),
                ),
            )
            _append_event_db(
                db,
                RunEvent(
                    event_id=event_id,
                    event_type="teamtask.assigned",
                    aggregate_type="team_task",
                    aggregate_id=task_id,
                    payload={"run_id": run_id, "agent_id": agent_row["agent_id"]},
                    occurred_at=now,
                ),
            )
            updated = db.execute(
                "SELECT * FROM team_tasks WHERE run_id = ? AND task_id = ?",
                (run_id, task_id),
            ).fetchone()
            return _task_from_row(updated), _agent_from_row(agent_row)

    def mark_team_task_working(
        self, run_id: str, task_id: str, *, event_id: str, now: datetime
    ) -> TeamTask:
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM team_tasks WHERE run_id = ? AND task_id = ?",
                (run_id, task_id),
            ).fetchone()
            if row is None:
                raise KeyError(task_id)
            if row["state"] == TeamTaskState.WORKING.value:
                return _task_from_row(row)
            if row["state"] != TeamTaskState.ASSIGNED.value:
                raise ValueError("task is not assigned")
            db.execute(
                "UPDATE team_tasks SET state = ?, updated_at = ? WHERE run_id = ? AND task_id = ?",
                (TeamTaskState.WORKING.value, now.isoformat(), run_id, task_id),
            )
            _append_event_db(
                db,
                RunEvent(
                    event_id=event_id,
                    event_type="teamtask.working",
                    aggregate_type="team_task",
                    aggregate_id=task_id,
                    payload={"run_id": run_id},
                    occurred_at=now,
                ),
            )
        return self.get_team_task(run_id, task_id)

    def complete_team_task(
        self,
        run_id: str,
        task_id: str,
        *,
        agent_id: str,
        artifacts: list[tuple[str, str, dict[str, object]]],
        idempotency_key: str,
        now: datetime,
    ) -> TeamTask:
        if not artifacts:
            raise ValueError("task completion requires evidence artifacts")
        event_id = f"teamrun:{run_id}:complete:{idempotency_key}"
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM team_tasks WHERE run_id = ? AND task_id = ?",
                (run_id, task_id),
            ).fetchone()
            if row is None:
                raise KeyError(task_id)
            if db.execute("SELECT 1 FROM run_events WHERE event_id = ?", (event_id,)).fetchone():
                return _task_from_row(row)
            if row["state"] != TeamTaskState.WORKING.value or row["assignee_agent_id"] != agent_id:
                raise ValueError("only the assigned working agent can complete the task")
            for index, (kind, uri, metadata) in enumerate(artifacts):
                existing = db.execute(
                    "SELECT 1 FROM artifacts WHERE run_id = ? AND task_id = ? "
                    "AND producer_agent_id = ? AND kind = ? AND uri = ? AND metadata_json = ?",
                    (
                        run_id,
                        task_id,
                        agent_id,
                        kind,
                        uri,
                        json.dumps(metadata, ensure_ascii=False, sort_keys=True),
                    ),
                ).fetchone()
                if existing is not None:
                    continue
                db.execute(
                    "INSERT INTO artifacts(artifact_id, run_id, task_id, producer_agent_id, "
                    "kind, uri, metadata_json, idempotency_key, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        f"artifact:{run_id}:{idempotency_key}:{index}",
                        run_id,
                        task_id,
                        agent_id,
                        kind,
                        uri,
                        json.dumps(metadata, ensure_ascii=False, sort_keys=True),
                        f"{idempotency_key}:{index}",
                        now.isoformat(),
                    ),
                )
            reviewer = db.execute(
                "SELECT agent_id FROM team_agents WHERE run_id = ? AND role = ?",
                (run_id, AgentRole.REVIEWER.value),
            ).fetchone()
            if reviewer is None:
                raise ValueError("run has no independent reviewer")
            db.execute(
                "UPDATE team_tasks SET state = ?, updated_at = ? WHERE run_id = ? AND task_id = ?",
                (TeamTaskState.REVIEW.value, now.isoformat(), run_id, task_id),
            )
            db.execute(
                "UPDATE write_leases SET released_at = ? WHERE run_id = ? AND task_id = ? "
                "AND released_at IS NULL",
                (now.isoformat(), run_id, task_id),
            )
            db.execute(
                "INSERT INTO mailbox_messages(message_id, run_id, task_id, sender_agent_id, "
                "recipient_agent_id, kind, body_json, idempotency_key, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    f"message:review:{event_id}",
                    run_id,
                    task_id,
                    agent_id,
                    reviewer["agent_id"],
                    "review_requested",
                    json.dumps(
                        {"artifact_count": len(artifacts), "attempt": row["attempt"]},
                        sort_keys=True,
                    ),
                    f"review:{idempotency_key}",
                    now.isoformat(),
                ),
            )
            _append_event_db(
                db,
                RunEvent(
                    event_id=event_id,
                    event_type="teamtask.review_requested",
                    aggregate_type="team_task",
                    aggregate_id=task_id,
                    payload={"run_id": run_id, "artifact_count": len(artifacts)},
                    occurred_at=now,
                ),
            )
        return self.get_team_task(run_id, task_id)

    def publish_team_artifact(
        self,
        run_id: str,
        task_id: str,
        *,
        agent_id: str,
        kind: str,
        uri: str,
        metadata: dict[str, object],
        idempotency_key: str,
        now: datetime,
    ) -> TeamArtifact:
        event_id = f"teamrun:{run_id}:artifact:{idempotency_key}"
        metadata_json = json.dumps(metadata, ensure_ascii=False, sort_keys=True)
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM team_tasks WHERE run_id = ? AND task_id = ?",
                (run_id, task_id),
            ).fetchone()
            if row is None:
                raise KeyError(task_id)
            existing = db.execute(
                "SELECT * FROM artifacts WHERE run_id = ? AND task_id = ? AND idempotency_key = ?",
                (run_id, task_id, idempotency_key),
            ).fetchone()
            if existing is not None:
                return _artifact_from_row(existing)
            if row["state"] != TeamTaskState.WORKING.value or row["assignee_agent_id"] != agent_id:
                raise ValueError("only the assigned working agent can publish an artifact")
            db.execute(
                "INSERT INTO artifacts(artifact_id, run_id, task_id, producer_agent_id, "
                "kind, uri, metadata_json, idempotency_key, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    f"artifact:{run_id}:{idempotency_key}",
                    run_id,
                    task_id,
                    agent_id,
                    kind,
                    uri,
                    metadata_json,
                    idempotency_key,
                    now.isoformat(),
                ),
            )
            _append_event_db(
                db,
                RunEvent(
                    event_id=event_id,
                    event_type="teamtask.artifact_published",
                    aggregate_type="team_task",
                    aggregate_id=task_id,
                    payload={
                        "run_id": run_id,
                        "agent_id": agent_id,
                        "kind": kind,
                        "uri": uri,
                    },
                    occurred_at=now,
                ),
            )
            created = db.execute(
                "SELECT * FROM artifacts WHERE run_id = ? AND task_id = ? AND idempotency_key = ?",
                (run_id, task_id, idempotency_key),
            ).fetchone()
            assert created is not None
            return _artifact_from_row(created)

    def review_team_task(
        self,
        run_id: str,
        task_id: str,
        *,
        reviewer_agent_id: str,
        verdict: str,
        notes: str,
        idempotency_key: str,
        now: datetime,
    ) -> TeamTask:
        if verdict not in {"approved", "rejected"}:
            raise ValueError("verdict must be approved or rejected")
        event_id = f"teamrun:{run_id}:review:{idempotency_key}"
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM team_tasks WHERE run_id = ? AND task_id = ?",
                (run_id, task_id),
            ).fetchone()
            if row is None:
                raise KeyError(task_id)
            if db.execute("SELECT 1 FROM run_events WHERE event_id = ?", (event_id,)).fetchone():
                return _task_from_row(row)
            reviewer = db.execute(
                "SELECT role FROM team_agents WHERE run_id = ? AND agent_id = ?",
                (run_id, reviewer_agent_id),
            ).fetchone()
            if (
                reviewer is None
                or reviewer["role"] != AgentRole.REVIEWER.value
                or reviewer_agent_id == row["assignee_agent_id"]
            ):
                raise ValueError("task acceptance requires an independent reviewer")
            if row["state"] != TeamTaskState.REVIEW.value:
                raise ValueError("task is not awaiting review")
            if verdict == "approved":
                next_state = TeamTaskState.ACCEPTED
            elif int(row["attempt"]) < int(row["max_attempts"]):
                next_state = TeamTaskState.RETRYING
            else:
                next_state = TeamTaskState.OPERATOR_REQUIRED
            db.execute(
                "UPDATE team_tasks SET state = ?, updated_at = ? WHERE run_id = ? AND task_id = ?",
                (next_state.value, now.isoformat(), run_id, task_id),
            )
            _append_event_db(
                db,
                RunEvent(
                    event_id=event_id,
                    event_type=f"teamtask.review_{verdict}",
                    aggregate_type="team_task",
                    aggregate_id=task_id,
                    payload={"run_id": run_id, "reviewer": reviewer_agent_id, "notes": notes},
                    occurred_at=now,
                ),
            )
            if next_state is TeamTaskState.OPERATOR_REQUIRED:
                db.execute(
                    "UPDATE team_runs SET state = ?, updated_at = ? WHERE run_id = ?",
                    (TeamRunState.OPERATOR_REQUIRED.value, now.isoformat(), run_id),
                )
            updated = db.execute(
                "SELECT * FROM team_tasks WHERE run_id = ? AND task_id = ?",
                (run_id, task_id),
            ).fetchone()
            return _task_from_row(updated)

    def block_team_task(
        self,
        run_id: str,
        task_id: str,
        *,
        agent_id: str,
        reason: str,
        idempotency_key: str,
        now: datetime,
    ) -> TeamTask:
        event_id = f"teamrun:{run_id}:blocked:{idempotency_key}"
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM team_tasks WHERE run_id = ? AND task_id = ?",
                (run_id, task_id),
            ).fetchone()
            if row is None:
                raise KeyError(task_id)
            if db.execute("SELECT 1 FROM run_events WHERE event_id = ?", (event_id,)).fetchone():
                return _task_from_row(row)
            if row["state"] != TeamTaskState.WORKING.value or row["assignee_agent_id"] != agent_id:
                raise ValueError("only the assigned working agent can block the task")
            db.execute(
                "UPDATE team_tasks SET state = ?, updated_at = ? WHERE run_id = ? AND task_id = ?",
                (TeamTaskState.BLOCKED.value, now.isoformat(), run_id, task_id),
            )
            db.execute(
                "UPDATE team_runs SET state = ?, updated_at = ? WHERE run_id = ?",
                (TeamRunState.OPERATOR_REQUIRED.value, now.isoformat(), run_id),
            )
            db.execute(
                "UPDATE write_leases SET released_at = ? WHERE run_id = ? AND task_id = ? "
                "AND released_at IS NULL",
                (now.isoformat(), run_id, task_id),
            )
            _append_event_db(
                db,
                RunEvent(
                    event_id=event_id,
                    event_type="teamtask.blocked",
                    aggregate_type="team_task",
                    aggregate_id=task_id,
                    payload={"run_id": run_id, "agent_id": agent_id, "reason": reason},
                    occurred_at=now,
                ),
            )
            updated = db.execute(
                "SELECT * FROM team_tasks WHERE run_id = ? AND task_id = ?",
                (run_id, task_id),
            ).fetchone()
            return _task_from_row(updated)

    def complete_run_if_accepted(self, run_id: str, *, now: datetime) -> bool:
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute("SELECT state FROM team_tasks WHERE run_id = ?", (run_id,)).fetchall()
            if not rows or any(row["state"] != TeamTaskState.ACCEPTED.value for row in rows):
                return False
            db.execute(
                "UPDATE team_runs SET state = ?, updated_at = ? WHERE run_id = ?",
                (TeamRunState.COMPLETED.value, now.isoformat(), run_id),
            )
            _append_event_db(
                db,
                RunEvent(
                    event_id=f"teamrun:{run_id}:completed",
                    event_type="teamrun.completed",
                    aggregate_type="team_run",
                    aggregate_id=run_id,
                    payload={},
                    occurred_at=now,
                ),
            )
            return True

    def stop_team_run(self, run_id: str, *, reason: str, event_id: str, now: datetime) -> None:
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM run_events WHERE event_id = ?", (event_id,)).fetchone():
                return
            db.execute(
                "UPDATE team_runs SET state = ?, updated_at = ? WHERE run_id = ?",
                (TeamRunState.STOPPED.value, now.isoformat(), run_id),
            )
            db.execute(
                "UPDATE team_tasks SET state = ?, updated_at = ? WHERE run_id = ? "
                "AND state IN (?, ?, ?)",
                (
                    TeamTaskState.BLOCKED.value,
                    now.isoformat(),
                    run_id,
                    TeamTaskState.READY.value,
                    TeamTaskState.ASSIGNED.value,
                    TeamTaskState.WORKING.value,
                ),
            )
            db.execute(
                "UPDATE write_leases SET released_at = ? WHERE run_id = ? AND released_at IS NULL",
                (now.isoformat(), run_id),
            )
            _append_event_db(
                db,
                RunEvent(
                    event_id=event_id,
                    event_type="teamrun.stopped",
                    aggregate_type="team_run",
                    aggregate_id=run_id,
                    payload={"reason": reason},
                    occurred_at=now,
                ),
            )

    def list_team_runs(self) -> list[TeamRun]:
        with self._connection() as db:
            rows = db.execute("SELECT * FROM team_runs ORDER BY created_at, rowid").fetchall()
        return [_run_from_row(row) for row in rows]

    def reconcile_team_runs(self, *, now: datetime) -> list[str]:
        operator_runs: list[str] = []
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            assigned = db.execute(
                "SELECT task.run_id, task.task_id FROM team_tasks AS task "
                "LEFT JOIN dispatch_commands AS command ON command.run_id = task.run_id "
                "AND command.task_id = task.task_id AND command.attempt = task.attempt "
                "WHERE task.state = ? AND (command.command_id IS NULL OR command.state = 'uncertain')",
                (TeamTaskState.ASSIGNED.value,),
            ).fetchall()
            for row in assigned:
                db.execute(
                    "UPDATE team_tasks SET state = ?, updated_at = ? "
                    "WHERE run_id = ? AND task_id = ?",
                    (
                        TeamTaskState.OPERATOR_REQUIRED.value,
                        now.isoformat(),
                        row["run_id"],
                        row["task_id"],
                    ),
                )
                db.execute(
                    "UPDATE team_runs SET state = ?, updated_at = ? WHERE run_id = ?",
                    (
                        TeamRunState.OPERATOR_REQUIRED.value,
                        now.isoformat(),
                        row["run_id"],
                    ),
                )
                _append_event_db(
                    db,
                    RunEvent(
                        event_id=f"teamrun:{row['run_id']}:reconcile:{row['task_id']}",
                        event_type="teamtask.dispatch_uncertain",
                        aggregate_type="team_task",
                        aggregate_id=row["task_id"],
                        payload={"run_id": row["run_id"], "action": "operator_required"},
                        occurred_at=now,
                    ),
                )
                operator_runs.append(row["run_id"])
        return operator_runs

    def upsert_provider_profile(self, profile: ProviderProfile) -> ProviderProfile:
        with self._connection() as db:
            existing = db.execute(
                "SELECT id FROM provider_profiles WHERE binary_name = ? AND executable_path = ?",
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
            cursor = db.execute("DELETE FROM provider_profiles WHERE id = ?", (provider_id,))
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
            row = db.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
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
            rows = db.execute("SELECT * FROM managed_sessions ORDER BY created_at, id").fetchall()
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
            cursor = db.execute("DELETE FROM managed_sessions WHERE id = ?", (session_id,))
            return cursor.rowcount == 1

    def has_active_teamrun_for_managed_session(self, session_id: str) -> bool:
        """A non-terminal plan retains both role and isolated worktree CLI identities."""
        with self._connection() as db:
            row = db.execute(
                "SELECT 1 FROM team_runs "
                "LEFT JOIN team_agents ON team_agents.run_id = team_runs.run_id "
                "LEFT JOIN task_worktrees ON task_worktrees.run_id = team_runs.run_id "
                "WHERE (team_agents.managed_session_id = ? "
                "OR (task_worktrees.managed_session_id = ? AND task_worktrees.state = 'active')) "
                "AND team_runs.state IN ('draft', 'running', 'paused', 'operator_required') "
                "LIMIT 1",
                (session_id, session_id),
            ).fetchone()
        return row is not None

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
                "SELECT * FROM probe_results WHERE provider_id = ? ORDER BY observed_at, id",
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


def _run_from_row(row: sqlite3.Row) -> TeamRun:
    return TeamRun(
        run_id=row["run_id"],
        goal=row["goal"],
        state=TeamRunState(row["state"]),
        max_retries=row["max_retries"],
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


def _agent_from_row(row: sqlite3.Row) -> TeamAgent:
    return TeamAgent(
        agent_id=row["agent_id"],
        run_id=row["run_id"],
        role=AgentRole(row["role"]),
        managed_session_id=row["managed_session_id"],
    )


def _task_from_row(row: sqlite3.Row) -> TeamTask:
    return TeamTask(
        task_id=row["task_id"],
        run_id=row["run_id"],
        title=row["title"],
        goal=row["goal"],
        role=AgentRole(row["role"]),
        state=TeamTaskState(row["state"]),
        dependencies=tuple(json.loads(row["dependencies_json"])),
        requires_write=bool(row["requires_write"]),
        max_attempts=row["max_attempts"],
        attempt=row["attempt"],
        assignee_agent_id=row["assignee_agent_id"],
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


def _message_from_row(row: sqlite3.Row) -> MailboxMessage:
    return MailboxMessage(
        message_id=row["message_id"],
        run_id=row["run_id"],
        task_id=row["task_id"],
        sender_agent_id=row["sender_agent_id"],
        recipient_agent_id=row["recipient_agent_id"],
        kind=row["kind"],
        body=json.loads(row["body_json"]),
        idempotency_key=row["idempotency_key"],
        created_at=datetime.fromisoformat(row["created_at"]),
        delivered_at=(
            datetime.fromisoformat(row["delivered_at"]) if row["delivered_at"] is not None else None
        ),
    )


def _artifact_from_row(row: sqlite3.Row) -> TeamArtifact:
    return TeamArtifact(
        artifact_id=row["artifact_id"],
        run_id=row["run_id"],
        task_id=row["task_id"],
        producer_agent_id=row["producer_agent_id"],
        kind=row["kind"],
        uri=row["uri"],
        metadata=json.loads(row["metadata_json"]),
        idempotency_key=row["idempotency_key"],
        created_at=datetime.fromisoformat(row["created_at"]),
    )


def _dispatch_command_from_row(row: sqlite3.Row) -> DispatchCommand:
    return DispatchCommand(
        command_id=row["command_id"],
        run_id=row["run_id"],
        task_id=row["task_id"],
        attempt=row["attempt"],
        managed_session_id=row["managed_session_id"],
        envelope=json.loads(row["envelope_json"]),
        state=row["state"],
        created_at=datetime.fromisoformat(row["created_at"]),
        tmux_written_at=(
            datetime.fromisoformat(row["tmux_written_at"]) if row["tmux_written_at"] else None
        ),
        last_error=row["last_error"],
    )


def _task_worktree_from_row(row: sqlite3.Row) -> TaskWorktreeRecord:
    return TaskWorktreeRecord(
        run_id=row["run_id"],
        task_id=row["task_id"],
        attempt=row["attempt"],
        path=row["path"],
        branch=row["branch"],
        managed_session_id=row["managed_session_id"],
        state=row["state"],
        created_at=datetime.fromisoformat(row["created_at"]),
        released_at=datetime.fromisoformat(row["released_at"]) if row["released_at"] else None,
    )


def _lease_from_row(row: sqlite3.Row) -> WriteLease:
    return WriteLease(
        lease_id=row["lease_id"],
        run_id=row["run_id"],
        task_id=row["task_id"],
        acquired_at=datetime.fromisoformat(row["acquired_at"]),
        released_at=(
            datetime.fromisoformat(row["released_at"]) if row["released_at"] is not None else None
        ),
    )


def _append_event_db(db: sqlite3.Connection, event: RunEvent) -> bool:
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


def _secure_regular_file(path: Path, *, create: bool = False, missing_ok: bool = False) -> None:
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
