import json
from pathlib import Path

from tmuxbot.backends.codex import CodexBackend
from tmuxbot.state import Binding


def _write_rollout(path: Path, cwd: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"type": "session_meta", "payload": {"cwd": cwd}}) + "\n",
        encoding="utf-8",
    )


def _binding(tmp_path: Path) -> Binding:
    return Binding(
        name="project",
        chat_id=1,
        thread_id=None,
        tmux_session="project",
        tmux_window=0,
        tmux_pane=0,
        cwd=tmp_path / "project",
        backend="codex",
        bot_token_env="TG_CODEX_BOT_TOKEN",
    )


def test_codex_find_active_jsonl_matches_binding_cwd(tmp_path, monkeypatch):
    sessions = tmp_path / "sessions"
    project = tmp_path / "project"
    project.mkdir()
    other = tmp_path / "other"
    other.mkdir()

    matching = sessions / "2026" / "06" / "19" / "rollout-project.jsonl"
    newest_other = sessions / "2026" / "06" / "19" / "rollout-other.jsonl"
    _write_rollout(matching, str(project))
    _write_rollout(newest_other, str(other))
    matching.touch()
    newest_other.touch()
    monkeypatch.setattr("tmuxbot.backends.codex.CODEX_SESSIONS_DIR", sessions)

    assert CodexBackend().find_active_jsonl(_binding(tmp_path)) == matching


def test_codex_find_active_jsonl_does_not_fallback_to_global_latest(tmp_path, monkeypatch):
    sessions = tmp_path / "sessions"
    project = tmp_path / "project"
    project.mkdir()
    other = tmp_path / "other"
    other.mkdir()

    newest_other = sessions / "2026" / "06" / "19" / "rollout-other.jsonl"
    _write_rollout(newest_other, str(other))
    monkeypatch.setattr("tmuxbot.backends.codex.CODEX_SESSIONS_DIR", sessions)

    assert CodexBackend().find_active_jsonl(_binding(tmp_path)) is None
