"""Compatibility reduction from normalized provider events to existing routes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from tmuxbot.core.events import ProviderEvent, ProviderEventKind


@dataclass(frozen=True, slots=True)
class ReducedEvent:
    kind: str
    body: str
    metadata: Mapping[str, Any] | None = None


def reduce_provider_event(event: ProviderEvent) -> list[ReducedEvent]:
    """Keep legacy delivery semantics while providers emit one shared contract."""
    if event.kind == ProviderEventKind.TEXT_DELTA:
        return [ReducedEvent("assistant_text_delta", event.text)]
    if event.kind == ProviderEventKind.FINAL_TEXT:
        route = "assistant_live_text" if event.phase == "live" else "assistant_text"
        return [ReducedEvent(route, event.text)]
    if event.kind == ProviderEventKind.TOOL_PROGRESS:
        metadata = {
            key: event.metadata[key]
            for key in ("tool_call_id", "tool_phase", "tool_name")
            if key in event.metadata
        }
        return [ReducedEvent("assistant_tools", event.text, metadata or None)]
    if event.kind == ProviderEventKind.PLAN_UPDATE:
        return [ReducedEvent("assistant_plan", event.text)]
    if event.kind == ProviderEventKind.INTERACTION_REQUEST:
        return [ReducedEvent("assistant_tools", event.text)]
    if event.kind == ProviderEventKind.PROVIDER_ERROR:
        return [ReducedEvent("assistant_tools", event.text)]
    if event.kind == ProviderEventKind.LIFECYCLE_CHANGE:
        lifecycle = event.metadata.get("lifecycle")
        if lifecycle in {"compaction_start", "compaction_end", "compaction_failed"}:
            return [ReducedEvent("provider_lifecycle", event.text)]
    # Other lifecycle and usage events update runtime state silently.
    return []
