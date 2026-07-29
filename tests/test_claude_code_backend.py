import asyncio
from pathlib import Path

from tmuxbot.backends.claude_code import ClaudeCodeBackend, _start_cmd
from tmuxbot.state import Binding


def test_start_cmd_uses_claude_bin_at_runtime(monkeypatch):
    monkeypatch.setenv("CLAUDE_BIN", "/opt/claude/bin/claude")

    assert _start_cmd() == "/opt/claude/bin/claude --dangerously-skip-permissions"


def test_claude_ensure_running_recreates_missing_tmux_and_resumes(monkeypatch, tmp_path):
    created = []
    launched = []
    binding = Binding(
        name="claude-lazy",
        chat_id=1,
        thread_id=None,
        tmux_session="claude-lazy",
        tmux_window=0,
        tmux_pane=0,
        cwd=Path(tmp_path),
        provider_session_id="claude-session-id",
    )

    monkeypatch.setattr("tmuxbot.backends.claude_code.tmux_has_session", lambda _name: False)
    monkeypatch.setattr(
        "tmuxbot.backends.claude_code.tmux_new_session",
        lambda name, cwd: created.append((name, cwd)),
    )
    monkeypatch.setattr("tmuxbot.backends.claude_code.tmux_pane_command", lambda _target: "bash")

    async def safe_launch(_target, command, *, allowed_shells):
        launched.append(command)
        return True

    async def no_sleep(_delay):
        return None

    monkeypatch.setattr("tmuxbot.backends.claude_code.tmux_safe_launch", safe_launch)
    monkeypatch.setattr("tmuxbot.backends.claude_code.asyncio.sleep", no_sleep)

    asyncio.run(ClaudeCodeBackend().ensure_running(binding))

    assert created == [("claude-lazy", tmp_path)]
    assert launched == [
        "claude --dangerously-skip-permissions --resume claude-session-id"
    ]
