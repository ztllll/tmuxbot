from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping


class RunState(str, Enum):
    DRAFT = "draft"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    OPERATOR_REQUIRED = "operator_required"


class TaskState(str, Enum):
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


class SessionClass(str, Enum):
    MANAGED = "managed"
    ORPHAN = "orphan"
    IGNORED = "ignored"


@dataclass(frozen=True, slots=True)
class RunEvent:
    event_id: str
    event_type: str
    aggregate_type: str
    aggregate_id: str
    payload: Mapping[str, Any]
    occurred_at: datetime
    sequence: int | None = None

    def __post_init__(self) -> None:
        if self.occurred_at.tzinfo is None or self.occurred_at.utcoffset() is None:
            raise ValueError("occurred_at must be timezone-aware")
        object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))


@dataclass(frozen=True, slots=True)
class TmuxPaneRecord:
    target: str
    session_name: str
    window_index: int
    pane_index: int
    command: str
    cwd: str
    pid: int


@dataclass(frozen=True, slots=True)
class SessionInventoryItem:
    pane: TmuxPaneRecord
    classification: SessionClass
    binding_name: str | None = None
    provider: str | None = None
    observed_at: datetime | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))
