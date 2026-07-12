import json
import os
from pathlib import Path

import yaml

from tmuxbot.backends.claude_code import ClaudeCodeBackend
from tmuxbot.backends.codex import CodexBackend
from tmuxbot.config import load_config, save_binding_identity
from tmuxbot.state import Binding, S


def _binding(tmp_path: Path, backend: str) -> Binding:
    return Binding(
        name=f"project-{backend}",
        chat_id=1,
        thread_id=None,
        tmux_session=f"project-{backend}",
        tmux_window=0,
        tmux_pane=0,
        cwd=tmp_path / "project",
        backend=backend,
        bot_token_env="TG_CODEX_BOT_TOKEN" if backend == "codex" else "TG_BOT_TOKEN",
    )


def _write_codex_rollout(path: Path, cwd: Path, session_id: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "type": "session_meta",
                "payload": {"id": session_id, "cwd": str(cwd)},
            }
        )
        + "\n",
        encoding="utf-8",
    )


def test_claude_prefers_pinned_transcript_for_same_cwd(tmp_path, monkeypatch):
    projects = tmp_path / "claude-projects"
    project_dir = projects / "encoded-project"
    project_dir.mkdir(parents=True)
    pinned = project_dir / "session-a.jsonl"
    other = project_dir / "session-b.jsonl"
    pinned.write_text("{}\n", encoding="utf-8")
    other.write_text("{}\n", encoding="utf-8")
    os.utime(pinned, (1, 1))
    os.utime(other, (2, 2))

    b = _binding(tmp_path, "claude_code")
    b.provider_session_id = "session-a"
    b.transcript_path = pinned
    monkeypatch.setattr("tmuxbot.backends.claude_code.CLAUDE_PROJECTS_DIR", projects)
    monkeypatch.setattr("tmuxbot.backends.claude_code.encode_cwd", lambda _cwd: "encoded-project")

    assert ClaudeCodeBackend().find_active_jsonl(b) == pinned


def test_claude_resolves_pinned_session_id_without_saved_path(tmp_path, monkeypatch):
    projects = tmp_path / "claude-projects"
    project_dir = projects / "encoded-project"
    project_dir.mkdir(parents=True)
    pinned = project_dir / "session-a.jsonl"
    other = project_dir / "session-b.jsonl"
    pinned.write_text("{}\n", encoding="utf-8")
    other.write_text("{}\n", encoding="utf-8")
    os.utime(pinned, (1, 1))
    os.utime(other, (2, 2))

    b = _binding(tmp_path, "claude_code")
    b.provider_session_id = "session-a"
    monkeypatch.setattr("tmuxbot.backends.claude_code.CLAUDE_PROJECTS_DIR", projects)
    monkeypatch.setattr("tmuxbot.backends.claude_code.encode_cwd", lambda _cwd: "encoded-project")

    assert ClaudeCodeBackend().find_active_jsonl(b) == pinned


def test_claude_pending_handoff_prefers_newer_project_transcript(tmp_path, monkeypatch):
    projects = tmp_path / "claude-projects"
    project_dir = projects / "encoded-project"
    project_dir.mkdir(parents=True)
    pinned = project_dir / "session-a.jsonl"
    newer = project_dir / "session-b.jsonl"
    pinned.write_text("{}\n", encoding="utf-8")
    newer.write_text("{}\n", encoding="utf-8")
    os.utime(pinned, (10, 10))
    os.utime(newer, (20, 20))

    b = _binding(tmp_path, "claude_code")
    b.provider_session_id = "session-a"
    b.transcript_path = pinned
    b.pending_session_handoff_after = 15.0
    monkeypatch.setattr("tmuxbot.backends.claude_code.CLAUDE_PROJECTS_DIR", projects)
    monkeypatch.setattr("tmuxbot.backends.claude_code.encode_cwd", lambda _cwd: "encoded-project")

    assert ClaudeCodeBackend().find_active_jsonl(b) == newer


def test_codex_prefers_pinned_transcript_for_same_cwd(tmp_path, monkeypatch):
    sessions = tmp_path / "codex-sessions"
    project = tmp_path / "project"
    project.mkdir()
    pinned = sessions / "2026" / "07" / "10" / "rollout-a.jsonl"
    other = sessions / "2026" / "07" / "10" / "rollout-b.jsonl"
    _write_codex_rollout(pinned, project, "session-a")
    _write_codex_rollout(other, project, "session-b")
    os.utime(pinned, (1, 1))
    os.utime(other, (2, 2))

    b = _binding(tmp_path, "codex")
    b.provider_session_id = "session-a"
    b.transcript_path = pinned
    monkeypatch.setattr("tmuxbot.backends.codex.CODEX_SESSIONS_DIR", sessions)

    assert CodexBackend().find_active_jsonl(b) == pinned


def test_codex_resolves_pinned_session_id_without_saved_path(tmp_path, monkeypatch):
    sessions = tmp_path / "codex-sessions"
    project = tmp_path / "project"
    project.mkdir()
    pinned = sessions / "2026" / "07" / "10" / "rollout-a.jsonl"
    other = sessions / "2026" / "07" / "10" / "rollout-b.jsonl"
    _write_codex_rollout(pinned, project, "session-a")
    _write_codex_rollout(other, project, "session-b")
    os.utime(pinned, (1, 1))
    os.utime(other, (2, 2))

    b = _binding(tmp_path, "codex")
    b.provider_session_id = "session-a"
    monkeypatch.setattr("tmuxbot.backends.codex.CODEX_SESSIONS_DIR", sessions)

    assert CodexBackend().find_active_jsonl(b) == pinned


def test_codex_pending_handoff_prefers_newer_same_cwd_transcript(tmp_path, monkeypatch):
    sessions = tmp_path / "codex-sessions"
    project = tmp_path / "project"
    project.mkdir()
    pinned = sessions / "2026" / "07" / "10" / "rollout-a.jsonl"
    newer = sessions / "2026" / "07" / "10" / "rollout-b.jsonl"
    _write_codex_rollout(pinned, project, "session-a")
    _write_codex_rollout(newer, project, "session-b")
    os.utime(pinned, (10, 10))
    os.utime(newer, (20, 20))

    b = _binding(tmp_path, "codex")
    b.provider_session_id = "session-a"
    b.transcript_path = pinned
    b.pending_session_handoff_after = 15.0
    monkeypatch.setattr("tmuxbot.backends.codex.CODEX_SESSIONS_DIR", sessions)

    assert CodexBackend().find_active_jsonl(b) == newer


def test_config_round_trips_binding_session_identity(tmp_path):
    env_file = tmp_path / ".env"
    bindings_file = tmp_path / "bindings.yaml"
    offsets_file = tmp_path / "offsets.json"
    transcript = tmp_path / "session-a.jsonl"
    env_file.write_text("BOSS_USER_ID=1\n", encoding="utf-8")
    offsets_file.write_text("{}", encoding="utf-8")
    bindings_file.write_text(
        yaml.safe_dump(
            {
                "bindings": [
                    {
                        "name": "project-claude",
                        "chat_id": 1,
                        "thread_id": None,
                        "tmux_session": "project-claude",
                        "tmux_window": 0,
                        "tmux_pane": 0,
                        "cwd": str(tmp_path / "project"),
                        "backend": "claude_code",
                        "bot_token_env": "TG_BOT_TOKEN",
                        "channel": "telegram",
                        "provider_session_id": "session-a",
                        "transcript_path": str(transcript),
                    }
                ]
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    load_config(env_file, bindings_file, offsets_file)

    assert S.bindings[0].provider_session_id == "session-a"
    assert S.bindings[0].transcript_path == transcript

    S.bindings[0].provider_session_id = "session-b"
    S.bindings[0].transcript_path = tmp_path / "session-b.jsonl"
    save_binding_identity(bindings_file, S.bindings[0])
    saved = yaml.safe_load(bindings_file.read_text(encoding="utf-8"))
    assert saved["bindings"][0]["provider_session_id"] == "session-b"
    assert saved["bindings"][0]["transcript_path"] == str(tmp_path / "session-b.jsonl")
