"""Compatibility reduction from normalized provider events to existing routes."""

from __future__ import annotations

from dataclasses import dataclass

from tmuxbot.core.events import ProviderEvent, ProviderEventKind


@dataclass(frozen=True, slots=True)
class ReducedEvent:
    kind: str
    body: str


def reduce_provider_event(event: ProviderEvent) -> list[ReducedEvent]:
    """Keep legacy delivery semantics while providers emit one shared contract."""
    if event.kind == ProviderEventKind.TEXT_DELTA:
        return [ReducedEvent("assistant_text_delta", event.text)]
    if event.kind == ProviderEventKind.FINAL_TEXT:
        route = "assistant_live_text" if event.phase == "live" else "assistant_text"
        return [ReducedEvent(route, event.text)]
    if event.kind == ProviderEventKind.TOOL_PROGRESS:
        return [ReducedEvent("assistant_tools", event.text)]
    if event.kind == ProviderEventKind.PLAN_UPDATE:
        return [ReducedEvent("assistant_plan", event.text)]
    if event.kind == ProviderEventKind.INTERACTION_REQUEST:
        return [ReducedEvent("assistant_tools", event.text)]
    if event.kind == ProviderEventKind.PROVIDER_ERROR:
        return [ReducedEvent("assistant_tools", event.text)]
    # Lifecycle and usage events update runtime state in v2; legacy channels stay silent.
    return []
