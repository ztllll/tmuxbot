from tmuxbot.backends.claude_code import _start_cmd


def test_start_cmd_uses_claude_bin_at_runtime(monkeypatch):
    monkeypatch.setenv("CLAUDE_BIN", "/opt/claude/bin/claude")

    assert _start_cmd() == "/opt/claude/bin/claude --dangerously-skip-permissions"

