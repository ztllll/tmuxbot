import json

import pytest

from tmuxbot.backends.claude_code import ClaudeCodeBackend
from tmuxbot.backends.codex import CodexBackend
from tmuxbot.core.events import ProviderEvent, ProviderEventKind


@pytest.mark.parametrize(
    ("backend", "line"),
    [
        (
            ClaudeCodeBackend(),
            {
                "type": "assistant",
                "uuid": "claude-message-1",
                "message": {"content": [{"type": "text", "text": "完成"}]},
            },
        ),
        (
            CodexBackend(),
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "id": "codex-message-1",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "完成"}],
                },
            },
        ),
    ],
)
def test_providers_normalize_final_text(backend, line):
    events = backend.parse_event(json.dumps(line), provider_session_id="session-1")

    assert len(events) == 1
    event = events[0]
    assert isinstance(event, ProviderEvent)
    assert event.kind == ProviderEventKind.FINAL_TEXT
    assert event.text == "完成"
    assert event.provider_session_id == "session-1"
    assert event.event_id.startswith(f"{backend.name}:session-1:")
    assert event.metadata["source"]["type"] == line["type"]


@pytest.mark.parametrize(
    ("backend", "line"),
    [
        (
            ClaudeCodeBackend(),
            {
                "type": "assistant",
                "uuid": "claude-tool-1",
                "message": {
                    "content": [
                        {"type": "tool_use", "id": "tool-1", "name": "Read", "input": {"file_path": "a.py"}}
                    ]
                },
            },
        ),
        (
            CodexBackend(),
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "call_id": "tool-1",
                    "name": "read_file",
                    "arguments": json.dumps({"path": "a.py"}),
                },
            },
        ),
    ],
)
def test_providers_normalize_tool_progress(backend, line):
    event = backend.parse_event(json.dumps(line), provider_session_id="session-1")[0]

    assert event.kind == ProviderEventKind.TOOL_PROGRESS
    assert "a.py" in event.text
    assert event.event_id.startswith(f"{backend.name}:session-1:")


@pytest.mark.parametrize(
    ("backend", "line"),
    [
        (
            ClaudeCodeBackend(),
            {
                "type": "assistant",
                "uuid": "claude-plan-1",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "plan-1",
                            "name": "TodoWrite",
                            "input": {"todos": [{"content": "修复", "status": "in_progress"}]},
                        }
                    ]
                },
            },
        ),
        (
            CodexBackend(),
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "call_id": "plan-1",
                    "name": "update_plan",
                    "arguments": json.dumps(
                        {"plan": [{"step": "修复", "status": "in_progress"}]},
                        ensure_ascii=False,
                    ),
                },
            },
        ),
    ],
)
def test_providers_normalize_plan_updates(backend, line):
    event = backend.parse_event(
        json.dumps(line, ensure_ascii=False), provider_session_id="session-1"
    )[0]

    assert event.kind == ProviderEventKind.PLAN_UPDATE
    assert "修复" in event.text


@pytest.mark.parametrize(
    ("backend", "line"),
    [
        (
            ClaudeCodeBackend(),
            {"type": "system", "subtype": "turn_duration", "duration_ms": 1200},
        ),
        (
            CodexBackend(),
            {"type": "event_msg", "payload": {"type": "task_complete"}},
        ),
    ],
)
def test_providers_normalize_lifecycle_events(backend, line):
    event = backend.parse_event(json.dumps(line), provider_session_id="session-1")[0]

    assert event.kind == ProviderEventKind.LIFECYCLE_CHANGE
    assert event.provider_session_id == "session-1"


def test_digest_event_ids_are_stable_without_native_ids():
    backend = CodexBackend()
    line = json.dumps(
        {"type": "event_msg", "payload": {"type": "agent_message_delta", "delta": "a"}}
    )

    first = backend.parse_event(line, provider_session_id="session-1")[0]
    second = backend.parse_event(line, provider_session_id="session-1")[0]

    assert first.event_id == second.event_id

