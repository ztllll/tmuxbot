"""Normalized provider events and terminal status models."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping


class ProviderEventKind(str, Enum):
    TEXT_DELTA = "text_delta"
    FINAL_TEXT = "final_text"
    TOOL_PROGRESS = "tool_progress"
    PLAN_UPDATE = "plan_update"
    INTERACTION_REQUEST = "interaction_request"
    LIFECYCLE_CHANGE = "lifecycle_change"
    USAGE_UPDATE = "usage_update"
    PROVIDER_ERROR = "provider_error"


class TerminalState(str, Enum):
    IDLE = "idle"
    WORKING = "working"
    WAITING = "waiting"
    BLOCKED = "blocked"
    DEAD = "dead"


@dataclass(frozen=True, slots=True)
class TerminalStatus:
    state: TerminalState
    label: str = ""
    model: str | None = None
    effort: str | None = None
    permission_mode: str | None = None
    cwd: str | None = None
    duration_seconds: float | None = None
    context_used: int | None = None
    context_limit: int | None = None
    blocked_reason: str | None = None


@dataclass(frozen=True, slots=True)
class ProviderEvent:
    event_id: str
    kind: ProviderEventKind
    text: str = ""
    status: TerminalStatus | None = None
    provider_session_id: str | None = None
    phase: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))
