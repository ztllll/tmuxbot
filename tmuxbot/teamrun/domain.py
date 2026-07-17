from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Iterable
from typing import Any, Mapping


class AgentRole(str, Enum):
    COORDINATOR = "coordinator"
    IMPLEMENTER = "implementer"
    REVIEWER = "reviewer"


class TeamRunState(str, Enum):
    DRAFT = "draft"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    OPERATOR_REQUIRED = "operator_required"
    STOPPED = "stopped"


class TeamTaskState(str, Enum):
    PENDING = "pending"
    READY = "ready"
    ASSIGNED = "assigned"
    WORKING = "working"
    REVIEW = "review"
    ACCEPTED = "accepted"
    BLOCKED = "blocked"
    FAILED = "failed"
    RETRYING = "retrying"
    OPERATOR_REQUIRED = "operator_required"


@dataclass(frozen=True, slots=True)
class TeamRun:
    run_id: str
    goal: str
    state: TeamRunState
    max_retries: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class TeamAgent:
    agent_id: str
    run_id: str
    role: AgentRole
    managed_session_id: str


@dataclass(frozen=True, slots=True)
class TeamTask:
    task_id: str
    run_id: str
    title: str
    goal: str
    role: AgentRole
    state: TeamTaskState
    dependencies: tuple[str, ...]
    requires_write: bool
    max_attempts: int
    attempt: int
    assignee_agent_id: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class TeamRunSnapshot:
    run: TeamRun
    agents: tuple[TeamAgent, ...]
    tasks: tuple[TeamTask, ...]


@dataclass(frozen=True, slots=True)
class MailboxMessage:
    message_id: str
    run_id: str
    task_id: str | None
    sender_agent_id: str | None
    recipient_agent_id: str | None
    kind: str
    body: Mapping[str, Any]
    idempotency_key: str
    created_at: datetime
    delivered_at: datetime | None


@dataclass(frozen=True, slots=True)
class TeamArtifact:
    artifact_id: str
    run_id: str
    task_id: str
    producer_agent_id: str
    kind: str
    uri: str
    metadata: Mapping[str, Any]
    idempotency_key: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class WriteLease:
    lease_id: str
    run_id: str
    task_id: str
    acquired_at: datetime
    released_at: datetime | None


@dataclass(frozen=True, slots=True)
class DispatchCommand:
    command_id: str
    run_id: str
    task_id: str
    attempt: int
    managed_session_id: str
    envelope: Mapping[str, Any]
    state: str
    created_at: datetime
    tmux_written_at: datetime | None
    last_error: str | None


def validate_task_graph(tasks: Iterable[TeamTask]) -> None:
    task_list = list(tasks)
    by_id = {task.task_id: task for task in task_list}
    if len(by_id) != len(task_list):
        raise ValueError("task ids must be unique")
    for task in task_list:
        for dependency in task.dependencies:
            if dependency not in by_id:
                raise ValueError(
                    f"task {task.task_id!r} references unknown dependency {dependency!r}"
                )
            if by_id[dependency].run_id != task.run_id:
                raise ValueError("task dependencies must belong to the same run")

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(task_id: str) -> None:
        if task_id in visiting:
            raise ValueError("task graph contains a cycle")
        if task_id in visited:
            return
        visiting.add(task_id)
        for dependency in by_id[task_id].dependencies:
            visit(dependency)
        visiting.remove(task_id)
        visited.add(task_id)

    for task_id in by_id:
        visit(task_id)
