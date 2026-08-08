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
class ProviderRuntimeMetadata:
    """Provider-authoritative fields used to enrich a captured terminal status."""

    provider: str | None = None
    model: str | None = None
    effort: str | None = None
    permission_mode: str | None = None
    session_name: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    cache_hit_rate: float | None = None
    cost_usd: float | None = None


@dataclass(frozen=True, slots=True)
class TerminalStatus:
    state: TerminalState
    label: str = ""
    provider: str | None = None
    model: str | None = None
    effort: str | None = None
    permission_mode: str | None = None
    cwd: str | None = None
    git_branch: str | None = None
    session_name: str | None = None
    duration_seconds: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    cache_hit_rate: float | None = None
    cost_usd: float | None = None
    subscription: bool | None = None
    extension_statuses: tuple[str, ...] = ()
    context_used: int | None = None
    context_limit: int | None = None
    context_percent: float | None = None
    auto_compact: bool | None = None
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
