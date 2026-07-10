from tmuxbot.backends.claude_code import ClaudeCodeBackend
from tmuxbot.backends.codex import CodexBackend
from tmuxbot.core.events import TerminalState


def test_provider_process_detection_and_safe_start_are_explicit():
    claude = ClaudeCodeBackend()
    codex = CodexBackend()

    assert claude.is_running_command("claude")
    assert codex.is_running_command("codex")
    assert codex.is_running_command("node")
    assert not claude.is_running_command("python3")
    assert claude.can_start_from_command("bash")
    assert codex.can_start_from_command("zsh")
    assert not claude.can_start_from_command("python3")
    assert not codex.can_start_from_command("claude")


def test_provider_capabilities_describe_real_provider_features():
    claude = ClaudeCodeBackend().capabilities
    codex = CodexBackend().capabilities

    assert claude.name == "claude_code"
    assert claude.supports_hooks
    assert claude.supports_tasks
    assert claude.supports_resume
    assert codex.name == "codex"
    assert codex.supports_incremental_text
    assert codex.supports_plans
    assert codex.supports_usage


def test_claude_terminal_status_normalizes_permission_and_context():
    status = ClaudeCodeBackend().parse_terminal_status(
        "383.6k/1m tokens (38%)\n"
        "new task? /clear to save 387.4k tokens\n"
        "⏵⏵ accept edits on (shift+tab to cycle) · ← for agents"
    )

    assert status is not None
    assert status.state == TerminalState.IDLE
    assert status.permission_mode == "accept edits"
    assert status.context_used == 383_600
    assert status.context_limit == 1_000_000
    assert "accept edits" in ClaudeCodeBackend().format_status_footer(status)


def test_codex_terminal_status_normalizes_working_model_and_cwd():
    status = CodexBackend().parse_terminal_status(
        "• Working (9s • esc to interrupt)\n"
        "gpt-5.6-sol high · ~/repo"
    )

    assert status is not None
    assert status.state == TerminalState.WORKING
    assert status.duration_seconds == 9
    assert status.model == "gpt-5.6-sol"
    assert status.effort == "high"
    assert status.cwd == "~/repo"
    assert CodexBackend().format_status_footer(status) == (
        "gpt-5.6-sol high · working 9s · ~/repo"
    )
