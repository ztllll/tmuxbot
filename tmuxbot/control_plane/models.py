from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping


PROVIDER_BINARIES = frozenset({"tmux", "claude", "codex"})


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
class ProviderProfile:
    id: str
    binary_name: str
    executable_path: str
    version: str | None
    device: int
    inode: int
    mtime_ns: int
    discovered_at: int

    def __post_init__(self) -> None:
        if self.binary_name not in PROVIDER_BINARIES:
            raise ValueError("provider binary is not allowlisted")
        if not self.id or not self.executable_path.startswith("/"):
            raise ValueError("provider identity and absolute executable path are required")


@dataclass(frozen=True, slots=True)
class ProjectRecord:
    id: str
    name: str
    root_path: str
    device: int
    inode: int
    mtime_ns: int
    created_at: int

    def __post_init__(self) -> None:
        if not self.id or not self.name.strip() or not self.root_path.startswith("/"):
            raise ValueError("project identity, name, and absolute root path are required")


@dataclass(frozen=True, slots=True)
class ManagedSession:
    id: str
    project_id: str
    provider_id: str
    name: str
    tmux_session: str
    tmux_window: int
    tmux_pane: int
    status: str
    created_at: int

    def __post_init__(self) -> None:
        if not all((self.id, self.project_id, self.provider_id, self.name, self.tmux_session)):
            raise ValueError("managed session identity fields are required")
        if self.tmux_window < 0 or self.tmux_pane < 0:
            raise ValueError("tmux window and pane must be non-negative")


@dataclass(frozen=True, slots=True)
class ProviderProbeResult:
    id: str
    provider_id: str
    success: bool
    version: str | None
    error_code: str | None
    exit_code: int | None
    duration_ms: int
    output_truncated: bool
    observed_at: int


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
