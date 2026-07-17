"""Versioned wire contract for tmux-backed multi-CLI workers.

The scheduler, worker CLI, and UI projections exchange these records instead
of treating prose injected into a terminal as an acknowledgement.  The module
is intentionally independent from storage and tmux so the contract can remain
stable while transports evolve.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Mapping

from tmuxbot.teamrun.domain import AgentRole


WORKER_PROTOCOL_VERSION = "tmuxbot.worker.v1"


class WorkerEventKind(str, Enum):
    TASK_CLAIMED = "task.claimed"
    TASK_PROGRESS = "task.progress"
    ARTIFACT_PUBLISHED = "artifact.published"
    TASK_COMPLETED = "task.completed"
    TASK_BLOCKED = "task.blocked"
    REVIEW_REQUESTED = "review.requested"
    REVIEW_COMPLETED = "review.completed"


class ReviewDecision(str, Enum):
    APPROVED = "approved"
    REJECTED = "rejected"


def _required_text(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value


def _string_list(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError(f"{field} must be a list")
    return tuple(_required_text(item, field) for item in value)


@dataclass(frozen=True, slots=True)
class ArtifactReference:
    """Evidence produced by a worker and stored by the control plane."""

    kind: str
    uri: str
    metadata: Mapping[str, object]

    def __post_init__(self) -> None:
        _required_text(self.kind, "artifact.kind")
        _required_text(self.uri, "artifact.uri")

    def to_wire(self) -> dict[str, object]:
        return {"kind": self.kind, "uri": self.uri, "metadata": dict(self.metadata)}

    @classmethod
    def from_wire(cls, value: object) -> ArtifactReference:
        if not isinstance(value, dict):
            raise ValueError("artifact must be an object")
        metadata = value.get("metadata", {})
        if not isinstance(metadata, dict):
            raise ValueError("artifact.metadata must be an object")
        return cls(
            kind=_required_text(value.get("kind"), "artifact.kind"),
            uri=_required_text(value.get("uri"), "artifact.uri"),
            metadata=metadata,
        )


@dataclass(frozen=True, slots=True)
class TaskAssignment:
    """A command sent by the scheduler to one managed tmux worker."""

    message_id: str
    run_id: str
    task_id: str
    attempt: int
    assignee_agent_id: str
    role: AgentRole
    goal: str
    constraints: tuple[str, ...]
    dependencies: tuple[str, ...]
    expected_artifacts: tuple[str, ...]
    acceptance_criteria: tuple[str, ...]
    idempotency_key: str
    protocol_version: str = WORKER_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        for field in (
            "message_id",
            "run_id",
            "task_id",
            "assignee_agent_id",
            "goal",
            "idempotency_key",
        ):
            _required_text(getattr(self, field), field)
        if self.protocol_version != WORKER_PROTOCOL_VERSION:
            raise ValueError("unsupported worker protocol version")
        if self.attempt < 1:
            raise ValueError("attempt must be at least 1")
        for field in (
            "constraints",
            "dependencies",
            "expected_artifacts",
            "acceptance_criteria",
        ):
            for item in getattr(self, field):
                _required_text(item, field)
        if not self.acceptance_criteria:
            raise ValueError("acceptance_criteria must not be empty")

    def to_wire(self) -> dict[str, object]:
        return {
            "protocol_version": self.protocol_version,
            "kind": "task.assignment",
            "message_id": self.message_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "attempt": self.attempt,
            "assignee_agent_id": self.assignee_agent_id,
            "role": self.role.value,
            "goal": self.goal,
            "constraints": list(self.constraints),
            "dependencies": list(self.dependencies),
            "expected_artifacts": list(self.expected_artifacts),
            "acceptance_criteria": list(self.acceptance_criteria),
            "idempotency_key": self.idempotency_key,
        }

    @classmethod
    def from_wire(cls, value: Mapping[str, object]) -> TaskAssignment:
        if value.get("kind") != "task.assignment":
            raise ValueError("expected task.assignment")
        return cls(
            message_id=_required_text(value.get("message_id"), "message_id"),
            run_id=_required_text(value.get("run_id"), "run_id"),
            task_id=_required_text(value.get("task_id"), "task_id"),
            attempt=value.get("attempt") if isinstance(value.get("attempt"), int) else 0,
            assignee_agent_id=_required_text(value.get("assignee_agent_id"), "assignee_agent_id"),
            role=AgentRole(_required_text(value.get("role"), "role")),
            goal=_required_text(value.get("goal"), "goal"),
            constraints=_string_list(value.get("constraints"), "constraints"),
            dependencies=_string_list(value.get("dependencies"), "dependencies"),
            expected_artifacts=_string_list(value.get("expected_artifacts"), "expected_artifacts"),
            acceptance_criteria=_string_list(
                value.get("acceptance_criteria"), "acceptance_criteria"
            ),
            idempotency_key=_required_text(value.get("idempotency_key"), "idempotency_key"),
            protocol_version=_required_text(value.get("protocol_version"), "protocol_version"),
        )


@dataclass(frozen=True, slots=True)
class ReviewRequest:
    """A scheduler command that hands evidence to an independent reviewer."""

    message_id: str
    run_id: str
    task_id: str
    attempt: int
    reviewer_agent_id: str
    producer_agent_id: str
    goal: str
    artifacts: tuple[ArtifactReference, ...]
    idempotency_key: str
    protocol_version: str = WORKER_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        for field in (
            "message_id",
            "run_id",
            "task_id",
            "reviewer_agent_id",
            "producer_agent_id",
            "goal",
            "idempotency_key",
        ):
            _required_text(getattr(self, field), field)
        if self.protocol_version != WORKER_PROTOCOL_VERSION:
            raise ValueError("unsupported worker protocol version")
        if self.attempt < 1:
            raise ValueError("attempt must be at least 1")
        if self.reviewer_agent_id == self.producer_agent_id:
            raise ValueError("reviewer must be independent from producer")
        if not self.artifacts:
            raise ValueError("review.requested requires artifacts")

    def to_wire(self) -> dict[str, object]:
        return {
            "protocol_version": self.protocol_version,
            "kind": WorkerEventKind.REVIEW_REQUESTED.value,
            "message_id": self.message_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "attempt": self.attempt,
            "reviewer_agent_id": self.reviewer_agent_id,
            "producer_agent_id": self.producer_agent_id,
            "goal": self.goal,
            "artifacts": [item.to_wire() for item in self.artifacts],
            "instructions": [
                "只读审查实现与证据",
                "明确给出 approved 或 rejected 及原因",
                "不得修改共享项目目录",
            ],
            "idempotency_key": self.idempotency_key,
        }


@dataclass(frozen=True, slots=True)
class WorkerEvent:
    """A worker's durable state report, suitable for an append-only audit log."""

    event_id: str
    kind: WorkerEventKind
    run_id: str
    task_id: str
    attempt: int
    actor_agent_id: str
    idempotency_key: str
    occurred_at: datetime
    evidence: tuple[ArtifactReference, ...] = ()
    message: str | None = None
    progress_percent: int | None = None
    review_decision: ReviewDecision | None = None
    protocol_version: str = WORKER_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        for field in ("event_id", "run_id", "task_id", "actor_agent_id", "idempotency_key"):
            _required_text(getattr(self, field), field)
        if self.protocol_version != WORKER_PROTOCOL_VERSION:
            raise ValueError("unsupported worker protocol version")
        if self.attempt < 1:
            raise ValueError("attempt must be at least 1")
        if self.occurred_at.tzinfo is None:
            raise ValueError("occurred_at must include a timezone")
        if self.progress_percent is not None and not 0 <= self.progress_percent <= 100:
            raise ValueError("progress_percent must be between 0 and 100")
        if self.kind is WorkerEventKind.TASK_PROGRESS and self.progress_percent is None:
            raise ValueError("task.progress requires progress_percent")
        if self.kind in {WorkerEventKind.ARTIFACT_PUBLISHED, WorkerEventKind.TASK_COMPLETED} and not self.evidence:
            raise ValueError(f"{self.kind.value} requires evidence")
        if self.kind is WorkerEventKind.TASK_BLOCKED:
            _required_text(self.message or "", "blocked message")
        if self.kind is WorkerEventKind.REVIEW_COMPLETED and self.review_decision is None:
            raise ValueError("review.completed requires review_decision")

    def to_wire(self) -> dict[str, object]:
        wire: dict[str, object] = {
            "protocol_version": self.protocol_version,
            "kind": self.kind.value,
            "event_id": self.event_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "attempt": self.attempt,
            "actor_agent_id": self.actor_agent_id,
            "idempotency_key": self.idempotency_key,
            "occurred_at": self.occurred_at.isoformat(),
            "evidence": [item.to_wire() for item in self.evidence],
        }
        if self.message is not None:
            wire["message"] = self.message
        if self.progress_percent is not None:
            wire["progress_percent"] = self.progress_percent
        if self.review_decision is not None:
            wire["review_decision"] = self.review_decision.value
        return wire

    @classmethod
    def from_wire(cls, value: Mapping[str, object]) -> WorkerEvent:
        evidence = value.get("evidence", [])
        if not isinstance(evidence, list):
            raise ValueError("evidence must be a list")
        occurred_at = value.get("occurred_at")
        if not isinstance(occurred_at, str):
            raise ValueError("occurred_at must be an ISO datetime")
        progress_percent = value.get("progress_percent")
        if progress_percent is not None and not isinstance(progress_percent, int):
            raise ValueError("progress_percent must be an integer")
        decision = value.get("review_decision")
        return cls(
            event_id=_required_text(value.get("event_id"), "event_id"),
            kind=WorkerEventKind(_required_text(value.get("kind"), "kind")),
            run_id=_required_text(value.get("run_id"), "run_id"),
            task_id=_required_text(value.get("task_id"), "task_id"),
            attempt=value.get("attempt") if isinstance(value.get("attempt"), int) else 0,
            actor_agent_id=_required_text(value.get("actor_agent_id"), "actor_agent_id"),
            idempotency_key=_required_text(value.get("idempotency_key"), "idempotency_key"),
            occurred_at=datetime.fromisoformat(occurred_at),
            evidence=tuple(ArtifactReference.from_wire(item) for item in evidence),
            message=value.get("message") if isinstance(value.get("message"), str) else None,
            progress_percent=progress_percent,
            review_decision=ReviewDecision(decision) if isinstance(decision, str) else None,
            protocol_version=_required_text(value.get("protocol_version"), "protocol_version"),
        )
