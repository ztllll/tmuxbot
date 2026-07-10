import json
from pathlib import Path

from tmuxbot.backends.claude_code import ClaudeCodeBackend
from tmuxbot.core.events import ProviderEventKind
from tmuxbot.hooks.claude import append_hook_payload, read_hook_spool
from tmuxbot.hooks.install import install_claude_hooks
from tmuxbot.state import Binding


FIXTURES = Path(__file__).parent / "fixtures" / "claude_hooks"


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _binding(tmp_path: Path) -> Binding:
    cwd = tmp_path / "claude-project"
    cwd.mkdir(exist_ok=True)
    return Binding(
        name="alpha-claude",
        chat_id=1,
        thread_id=None,
        tmux_session="alpha-claude",
        tmux_window=0,
        tmux_pane=0,
        cwd=cwd,
        backend="claude_code",
    )


def test_stop_hook_uses_last_assistant_message_as_final_text(tmp_path):
    backend = ClaudeCodeBackend(hook_spool_path=tmp_path / "hooks.jsonl")
    payload = _fixture("stop.json")
    payload["cwd"] = str(tmp_path / "claude-project")
    payload["transcript_path"] = str(tmp_path / "claude-project" / "session-claude-1.jsonl")

    events = backend.parse_hook_payload(payload)

    assert len(events) == 1
    event = events[0]
    assert event.kind == ProviderEventKind.FINAL_TEXT
    assert event.text == "修复已经完成。"
    assert event.provider_session_id == "session-claude-1"
    assert event.metadata["hook_event_name"] == "Stop"


def test_later_transcript_copy_of_hook_final_is_suppressed(tmp_path):
    backend = ClaudeCodeBackend(hook_spool_path=tmp_path / "hooks.jsonl")
    payload = _fixture("stop.json")
    backend.parse_hook_payload(payload)
    transcript_line = json.dumps(
        {
            "type": "assistant",
            "uuid": "message-1",
            "message": {"content": [{"type": "text", "text": "修复已经完成。"}]},
        },
        ensure_ascii=False,
    )

    assert backend.parse_event(
        transcript_line, provider_session_id="session-claude-1"
    ) == []


def test_session_start_pins_binding_identity(tmp_path):
    backend = ClaudeCodeBackend(hook_spool_path=tmp_path / "hooks.jsonl")
    b = _binding(tmp_path)
    transcript = b.cwd / "session-claude-1.jsonl"
    payload = _fixture("session_start.json")
    payload["cwd"] = str(b.cwd)
    payload["transcript_path"] = str(transcript)

    events = backend.parse_hook_payload(payload, binding=b)

    assert events[0].kind == ProviderEventKind.LIFECYCLE_CHANGE
    assert b.provider_session_id == "session-claude-1"
    assert b.last_session_id == "session-claude-1"
    assert b.transcript_path == transcript


def test_hook_spool_append_is_jsonl_and_offset_safe(tmp_path):
    spool = tmp_path / "claude-hooks.jsonl"
    first = _fixture("session_start.json")
    second = _fixture("notification.json")

    append_hook_payload(first, spool)
    first_batch, offset = read_hook_spool(spool, 0)
    append_hook_payload(second, spool)
    second_batch, final_offset = read_hook_spool(spool, offset)

    assert first_batch == [first]
    assert second_batch == [second]
    assert final_offset == spool.stat().st_size


def test_claude_backend_polls_new_matching_hook_events(tmp_path):
    spool = tmp_path / "claude-hooks.jsonl"
    backend = ClaudeCodeBackend(hook_spool_path=spool)
    b = _binding(tmp_path)

    assert backend.poll_provider_events(b) == []
    payload = _fixture("stop.json")
    payload["cwd"] = str(b.cwd)
    payload["transcript_path"] = str(b.cwd / "session-claude-1.jsonl")
    append_hook_payload(payload, spool)

    events = backend.poll_provider_events(b)

    assert [event.kind for event in events] == [ProviderEventKind.FINAL_TEXT]
    assert b.provider_session_id == "session-claude-1"
    assert backend.poll_provider_events(b) == []


def test_hook_installer_is_idempotent_and_preserves_unrelated_settings(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text(
        json.dumps(
            {
                "model": "opus",
                "hooks": {
                    "Stop": [
                        {
                            "matcher": "",
                            "hooks": [{"type": "command", "command": "notify-send done"}],
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )

    first = install_claude_hooks(settings_path=settings)
    second = install_claude_hooks(settings_path=settings)
    saved = json.loads(settings.read_text(encoding="utf-8"))

    assert first == second == saved
    assert saved["model"] == "opus"
    stop_commands = [
        hook["command"]
        for matcher in saved["hooks"]["Stop"]
        for hook in matcher.get("hooks", [])
    ]
    assert "notify-send done" in stop_commands
    assert sum("tmuxbot.hooks.claude" in command for command in stop_commands) == 1


def test_hook_installer_dry_run_does_not_write(tmp_path):
    settings = tmp_path / "settings.json"

    merged = install_claude_hooks(settings_path=settings, dry_run=True)

    assert not settings.exists()
    assert "SessionStart" in merged["hooks"]
    assert "StopFailure" in merged["hooks"]
