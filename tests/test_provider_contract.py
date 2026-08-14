import json
from pathlib import Path

from tmuxbot.backends import claude_code, codex
from tmuxbot.backends.claude_code import ClaudeCodeBackend
from tmuxbot.backends.codex import CodexBackend
from tmuxbot.backends.omp import OmpBackend
from tmuxbot.core.events import TerminalState
from tmuxbot.state import Binding


def _binding(tmp_path: Path, backend: str) -> Binding:
    return Binding(
        name="provider-test",
        chat_id=1,
        thread_id=None,
        tmux_session="provider-test",
        tmux_window=0,
        tmux_pane=0,
        cwd=tmp_path,
        backend=backend,
    )


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
    omp = OmpBackend().capabilities

    assert claude.name == "claude_code"
    assert claude.supports_hooks
    assert claude.supports_tasks
    assert claude.supports_resume
    assert codex.name == "codex"
    assert codex.supports_incremental_text
    assert codex.supports_plans
    assert codex.supports_usage
    assert omp.accepts_input_while_busy


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
        "• Working (9s • esc to interrupt)\ngpt-5.6-sol high · ~/repo · Main [default]"
    )

    assert status is not None
    assert status.state == TerminalState.WORKING
    assert status.duration_seconds == 9
    assert status.model == "gpt-5.6-sol"
    assert status.effort == "high"
    assert status.cwd == "~/repo"
    assert CodexBackend().format_status_footer(status) == ("gpt-5.6-sol high · working 9s · ~/repo")


def test_codex_terminal_status_keeps_effort_when_cwd_is_home():
    status = CodexBackend().parse_terminal_status("gpt-5.6-terra medium · ~ · Main [default]")

    assert status is not None
    assert status.model == "gpt-5.6-terra"
    assert status.effort == "medium"
    assert status.cwd == "~"
    assert CodexBackend().format_status_footer(status) == "gpt-5.6-terra medium · ~"


def test_codex_terminal_status_renders_xhigh_effort():
    status = CodexBackend().parse_terminal_status("gpt-5.6-sol xhigh · ~/repo · feature/footer")

    assert status is not None
    assert status.effort == "xhigh"
    assert CodexBackend().format_status_footer(status) == "gpt-5.6-sol xhigh · ~/repo"


def test_codex_terminal_status_omits_missing_effort_cleanly():
    status = CodexBackend().parse_terminal_status("gpt-5.6-sol · ~/repo · Main [default]")

    assert status is not None
    assert status.effort is None
    assert CodexBackend().format_status_footer(status) == "gpt-5.6-sol · ~/repo"


def test_codex_runtime_metadata_falls_back_to_active_transcript(tmp_path, monkeypatch):
    sessions = tmp_path / "codex-sessions"
    rollout = sessions / "2026" / "07" / "12" / "rollout-test.jsonl"
    rollout.parent.mkdir(parents=True)
    rollout.write_text(
        "\n".join(
            (
                json.dumps(
                    {"type": "session_meta", "payload": {"id": "s-1", "cwd": str(tmp_path)}}
                ),
                json.dumps(
                    {
                        "type": "event_msg",
                        "payload": {
                            "type": "thread_settings_applied",
                            "thread_settings": {
                                "model": "gpt-5.6-terra",
                                "reasoning_effort": "medium",
                            },
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "turn_context",
                        "payload": {
                            "model": "gpt-5.6-terra",
                            "effort": "medium",
                            "cwd": str(tmp_path),
                        },
                    }
                ),
            )
        )
        + "\n"
    )
    monkeypatch.setattr(codex, "CODEX_SESSIONS_DIR", sessions)

    backend = CodexBackend()
    binding = _binding(tmp_path, "codex")
    metadata = backend.current_runtime_metadata(binding)
    assert metadata.model == "gpt-5.6-terra"
    assert metadata.effort == "medium"

    with rollout.open("a", encoding="utf-8") as stream:
        stream.write(
            json.dumps(
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "thread_settings_applied",
                        "thread_settings": {
                            "model": "gpt-5.6-sol",
                            "reasoning_effort": "high",
                        },
                    },
                }
            )
            + "\n"
        )

    updated = backend.current_runtime_metadata(binding)
    assert updated.model == "gpt-5.6-sol"
    assert updated.effort == "high"


def test_codex_runtime_metadata_does_not_reuse_stale_effort(tmp_path, monkeypatch):
    sessions = tmp_path / "codex-sessions"
    rollout = sessions / "2026" / "07" / "12" / "rollout-test.jsonl"
    rollout.parent.mkdir(parents=True)
    rollout.write_text(
        "\n".join(
            (
                json.dumps(
                    {
                        "type": "session_meta",
                        "payload": {"id": "s-1", "cwd": str(tmp_path)},
                    }
                ),
                json.dumps(
                    {
                        "type": "event_msg",
                        "payload": {
                            "type": "thread_settings_applied",
                            "thread_settings": {
                                "model": "gpt-5.6-terra",
                                "reasoning_effort": "high",
                            },
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "event_msg",
                        "payload": {
                            "type": "thread_settings_applied",
                            "thread_settings": {"model": "gpt-5.6-sol"},
                        },
                    }
                ),
            )
        )
        + "\n"
    )
    monkeypatch.setattr(codex, "CODEX_SESSIONS_DIR", sessions)

    metadata = CodexBackend().current_runtime_metadata(_binding(tmp_path, "codex"))

    assert metadata.model == "gpt-5.6-sol"
    assert metadata.effort is None


def test_codex_permission_mode_falls_back_to_cli_launch_flag(tmp_path, monkeypatch):
    monkeypatch.setattr(
        codex,
        "tmux_pane_process_commands",
        lambda target: ("codex resume --dangerously-bypass-approvals-and-sandbox session-id",),
    )

    assert CodexBackend().current_permission_mode(_binding(tmp_path, "codex")) == "YOLO"


def test_claude_current_model_falls_back_to_active_transcript(tmp_path, monkeypatch):
    projects = tmp_path / "claude-projects"
    monkeypatch.setattr(claude_code, "CLAUDE_PROJECTS_DIR", projects)
    project = projects / claude_code.encode_cwd(tmp_path)
    project.mkdir(parents=True)
    transcript = project / "session-1.jsonl"
    transcript.write_text(
        json.dumps({"type": "assistant", "message": {"model": "claude-opus-4-8"}}) + "\n"
    )

    assert ClaudeCodeBackend().current_model(_binding(tmp_path, "claude_code")) == "claude-opus-4-8"


def test_claude_current_effort_falls_back_to_active_transcript(tmp_path, monkeypatch):
    projects = tmp_path / "claude-projects"
    monkeypatch.setattr(claude_code, "CLAUDE_PROJECTS_DIR", projects)
    project = projects / claude_code.encode_cwd(tmp_path)
    project.mkdir(parents=True)
    transcript = project / "session-1.jsonl"
    transcript.write_text(
        "\n".join(
            (
                json.dumps(
                    {
                        "type": "assistant",
                        "effort": "high",
                        "message": {"model": "claude-opus-4-8"},
                    }
                ),
                json.dumps({"type": "user", "message": {"content": "继续"}}),
            )
        )
        + "\n"
    )

    backend = ClaudeCodeBackend()
    binding = _binding(tmp_path, "claude_code")
    assert backend.current_effort(binding) == "high"

    with transcript.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"type": "assistant", "effort": "low"}) + "\n")

    assert backend.current_effort(binding) == "low"


def test_claude_current_effort_ignores_newer_sidechain_rows(tmp_path, monkeypatch):
    projects = tmp_path / "claude-projects"
    monkeypatch.setattr(claude_code, "CLAUDE_PROJECTS_DIR", projects)
    project = projects / claude_code.encode_cwd(tmp_path)
    project.mkdir(parents=True)
    (project / "session-1.jsonl").write_text(
        "\n".join(
            (
                json.dumps({"type": "assistant", "effort": "high"}),
                json.dumps(
                    {
                        "type": "assistant",
                        "effort": "low",
                        "isSidechain": True,
                    }
                ),
            )
        )
        + "\n"
    )

    assert ClaudeCodeBackend().current_effort(_binding(tmp_path, "claude_code")) == "high"


def test_claude_current_model_prefers_latest_context_usage_model(tmp_path, monkeypatch):
    projects = tmp_path / "claude-projects"
    monkeypatch.setattr(claude_code, "CLAUDE_PROJECTS_DIR", projects)
    project = projects / claude_code.encode_cwd(tmp_path)
    project.mkdir(parents=True)
    (project / "session-1.jsonl").write_text(
        "\n".join(
            (
                json.dumps({"type": "assistant", "message": {"model": "claude-opus-4-8"}}),
                json.dumps(
                    {
                        "type": "user",
                        "message": {
                            "content": "## Context Usage\\n\\n**Model:** claude-fable-5  \\n"
                        },
                    }
                ),
            )
        )
        + "\n"
    )

    assert ClaudeCodeBackend().current_model(_binding(tmp_path, "claude_code")) == "claude-fable-5"
