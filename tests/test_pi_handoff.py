import json
from pathlib import Path

from tmuxbot.backends import pi
from tmuxbot.backends.pi import PiBackend, encode_pi_cwd
from tmuxbot.runtime.pi_handoff import handoff_record_path, read_handoff
from tmuxbot.state import Binding


def route(cwd: Path) -> Binding:
    return Binding(
        name="route",
        chat_id=1,
        thread_id=None,
        tmux_session="project",
        tmux_window=0,
        tmux_pane=0,
        cwd=cwd,
        backend="pi",
    )


def write_session(path: Path, cwd: Path, session_id: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"type": "session", "id": session_id, "cwd": str(cwd)}) + "\n",
        encoding="utf-8",
    )


def write_handoff(target: str, cwd: Path, session_id: str, transcript: Path) -> None:
    path = handoff_record_path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "tmuxTarget": target,
                "cwd": str(cwd),
                "sessionId": session_id,
                "transcriptPath": str(transcript),
            }
        ),
        encoding="utf-8",
    )


def test_read_handoff_requires_exact_target_and_cwd(tmp_path, monkeypatch):
    monkeypatch.setenv("TMUXBOT_STATE_DIR", str(tmp_path / "state"))
    cwd = tmp_path / "project"
    cwd.mkdir()
    transcript = tmp_path / "sessions" / "new.jsonl"
    write_session(transcript, cwd, "new")
    write_handoff("project:0.0", cwd, "new", transcript)

    handoff = read_handoff("project:0.0", cwd)

    assert handoff is not None
    assert handoff.session_id == "new"
    assert handoff.transcript_path == transcript
    assert read_handoff("other:0.0", cwd) is None
    assert read_handoff("project:0.0", tmp_path / "other") is None


def test_pi_backend_adopts_provider_authored_handoff_before_stale_pin(tmp_path, monkeypatch):
    monkeypatch.setenv("TMUXBOT_STATE_DIR", str(tmp_path / "state"))
    sessions = tmp_path / "sessions"
    monkeypatch.setattr(pi, "PI_SESSIONS_DIR", sessions)
    cwd = tmp_path / "project"
    cwd.mkdir()
    old = sessions / encode_pi_cwd(cwd) / "old.jsonl"
    new = sessions / encode_pi_cwd(cwd) / "new.jsonl"
    write_session(old, cwd, "old")
    write_session(new, cwd, "new")
    item = route(cwd)
    item.provider_session_id = "old"
    item.transcript_path = old
    write_handoff(item.tmux_target, cwd, "new", new)

    assert PiBackend().find_active_jsonl(item) == new
