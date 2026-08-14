from tmuxbot.core.event_reducer import ReducedEvent, reduce_provider_event
from tmuxbot.core.events import ProviderEvent, ProviderEventKind


def test_compatibility_reducer_maps_normalized_events_to_existing_routes():
    event = ProviderEvent(
        event_id="codex:s1:1",
        kind=ProviderEventKind.FINAL_TEXT,
        text="done",
        provider_session_id="s1",
    )

    assert reduce_provider_event(event) == [ReducedEvent(kind="assistant_text", body="done")]


def test_compatibility_reducer_preserves_tool_slot_identity():
    event = ProviderEvent(
        event_id="omp:s1:tool",
        kind=ProviderEventKind.TOOL_PROGRESS,
        text="editing",
        provider_session_id="s1",
        metadata={
            "tool_call_id": "call-edit",
            "tool_phase": "start",
            "tool_name": "edit",
            "source": {"large": "provider row must not cross the reducer"},
        },
    )

    assert reduce_provider_event(event) == [
        ReducedEvent(
            kind="assistant_tools",
            body="editing",
            metadata={
                "tool_call_id": "call-edit",
                "tool_phase": "start",
                "tool_name": "edit",
            },
        )
    ]


def test_compatibility_reducer_keeps_lifecycle_events_internal():
    event = ProviderEvent(
        event_id="codex:s1:2",
        kind=ProviderEventKind.LIFECYCLE_CHANGE,
        text="complete",
        provider_session_id="s1",
    )

    assert reduce_provider_event(event) == []
