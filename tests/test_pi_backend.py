import json
from pathlib import Path

from tmuxbot.backends import pi
from tmuxbot.backends.pi import PiBackend, encode_pi_cwd
from tmuxbot.core.events import ProviderEventKind, TerminalState
from tmuxbot.state import Binding


def binding(cwd: Path) -> Binding:
    return Binding(
        name="pi-route",
        chat_id=1,
        thread_id=None,
        tmux_session="pi-route",
        tmux_window=0,
        tmux_pane=0,
        cwd=cwd,
        backend="pi",
    )


def write_session(path: Path, cwd: Path, session_id: str = "session-1") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            (
                json.dumps(
                    {
                        "type": "session",
                        "version": 3,
                        "id": session_id,
                        "cwd": str(cwd),
                    }
                ),
                json.dumps(
                    {
                        "type": "model_change",
                        "provider": "openai",
                        "modelId": "gpt-5.6-sol",
                    }
                ),
                json.dumps(
                    {
                        "type": "thinking_level_change",
                        "thinkingLevel": "high",
                    }
                ),
            )
        )
        + "\n",
        encoding="utf-8",
    )


def test_pi_cwd_encoding_matches_pi_session_manager():
    assert encode_pi_cwd(Path("/home/user/project:alpha")) == "--home-user-project-alpha--"


def test_pi_finds_only_sessions_whose_header_matches_route_cwd(tmp_path, monkeypatch):
    sessions_root = tmp_path / "sessions"
    monkeypatch.setattr(pi, "PI_SESSIONS_DIR", sessions_root)
    cwd = tmp_path / "repo"
    wanted = sessions_root / encode_pi_cwd(cwd) / "2026-session.jsonl"
    write_session(wanted, cwd)
    wrong = sessions_root / encode_pi_cwd(cwd) / "newer-wrong.jsonl"
    write_session(wrong, tmp_path / "other", session_id="wrong")
    wrong.touch()

    backend = PiBackend()

    assert backend.find_active_jsonl(binding(cwd)) == wanted
    assert backend.session_identity(binding(cwd), wanted).session_id == "session-1"


def test_pi_new_session_handoff_adopts_only_newer_matching_session(tmp_path, monkeypatch):
    sessions_root = tmp_path / "sessions"
    monkeypatch.setattr(pi, "PI_SESSIONS_DIR", sessions_root)
    cwd = tmp_path / "repo"
    old = sessions_root / encode_pi_cwd(cwd) / "old.jsonl"
    write_session(old, cwd, session_id="old")
    route = binding(cwd)
    route.provider_session_id = "old"
    route.transcript_path = old
    route.pending_session_handoff_after = old.stat().st_mtime + 0.01

    newer = sessions_root / encode_pi_cwd(cwd) / "new.jsonl"
    write_session(newer, cwd, session_id="new")
    stamp = route.pending_session_handoff_after + 1
    newer.touch()
    import os
    os.utime(newer, (stamp, stamp))

    assert PiBackend().find_active_jsonl(route) == newer


def test_pi_parser_normalizes_text_thinking_and_tool_calls():
    row = {
        "type": "message",
        "id": "entry-1",
        "message": {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "check the repository"},
                {
                    "type": "toolCall",
                    "id": "call-1",
                    "name": "bash",
                    "arguments": {"command": "git status --short"},
                },
                {"type": "text", "text": "Ready <now>"},
            ],
            "stopReason": "stop",
        },
    }

    events = PiBackend().parse_event(json.dumps(row), "session-1")

    assert [event.kind for event in events] == [
        ProviderEventKind.TOOL_PROGRESS,
        ProviderEventKind.FINAL_TEXT,
    ]
    assert "check the repository" in events[0].text
    assert "git status --short" in events[0].text
    assert events[1].text == "Ready &lt;now&gt;"


def test_pi_parser_ignores_user_and_tool_result_messages():
    backend = PiBackend()
    for role in ("user", "toolResult"):
        assert backend.parse_event(
            json.dumps({"type": "message", "message": {"role": role, "content": []}})
        ) == []


def test_pi_runtime_metadata_and_usage_come_from_transcript(tmp_path, monkeypatch):
    sessions_root = tmp_path / "sessions"
    monkeypatch.setattr(pi, "PI_SESSIONS_DIR", sessions_root)
    cwd = tmp_path / "repo"
    transcript = sessions_root / encode_pi_cwd(cwd) / "session.jsonl"
    write_session(transcript, cwd)
    with transcript.open("a", encoding="utf-8") as stream:
        stream.write(
            json.dumps(
                {
                    "type": "message",
                    "timestamp": "2026-08-08T01:00:00Z",
                    "message": {
                        "role": "assistant",
                        "provider": "openai",
                        "model": "gpt-5.6-sol",
                        "usage": {
                            "input": 100,
                            "output": 20,
                            "cacheRead": 50,
                            "cacheWrite": 5,
                            "reasoning": 7,
                            "totalTokens": 182,
                        },
                        "content": [{"type": "text", "text": "done"}],
                    },
                }
            )
            + "\n"
        )

    backend = PiBackend()
    route = binding(cwd)

    metadata = backend.current_runtime_metadata(route)
    assert metadata.model == "gpt-5.6-sol"
    assert metadata.effort == "high"
    assert backend.aggregate_usage(transcript) == {
        "count": 1,
        "input": 100,
        "output": 20,
        "cache_create": 5,
        "cache_read": 50,
        "cache_hit_rate": 50 / 155,
        "last_ts": "2026-08-08T01:00:00Z",
        "model": "gpt-5.6-sol",
    }


def test_pi_terminal_status_recognizes_working_and_footer_metadata():
    status = PiBackend().parse_terminal_status(
        "⠧ Working...\n"
        "~/repo (main)\n"
        "↑1.3M ↓98k R19M CH84.2% 8.0%/1.1M (auto)        gpt-5.6-sol • high"
    )

    assert status is not None
    assert status.state == TerminalState.WORKING
    assert status.model == "gpt-5.6-sol"
    assert status.effort == "high"
    assert status.cwd == "~/repo"
    assert status.context_used == 88_000
    assert status.context_limit == 1_100_000
