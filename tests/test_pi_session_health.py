import json
from pathlib import Path

from tmuxbot.runtime import pi_session_health


def write_session(path: Path, cwd: Path, session_id: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"type": "session", "id": session_id, "cwd": str(cwd)}) + "\n",
        encoding="utf-8",
    )


def test_read_session_health_validates_exact_unicode_target_cwd_and_session_header(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("TMUXBOT_STATE_DIR", str(tmp_path / "state"))
    cwd = tmp_path / "project"
    cwd.mkdir()
    transcript = tmp_path / "sessions" / "session.jsonl"
    write_session(transcript, cwd, "session-1")
    target = "pi-中文:0.0"
    record = pi_session_health.session_health_record_path(target)
    record.parent.mkdir(parents=True)
    record.write_text(
        json.dumps(
            {
                "version": 1,
                "tmuxTarget": target,
                "cwd": str(cwd),
                "sessionId": "session-1",
                "transcriptPath": str(transcript),
                "state": "terminal_error",
                "observedAt": "2026-08-12T00:00:00.000Z",
                "error": {"message": "503 unavailable", "responseId": "resp-1"},
            }
        ),
        encoding="utf-8",
    )

    health = pi_session_health.read_session_health(target, cwd)

    assert health is not None
    assert health.state == "terminal_error"
    assert health.session_id == "session-1"
    assert health.error_message == "503 unavailable"
    assert record.name.isascii()


def test_read_session_health_fails_closed_for_symlink_or_header_mismatch(tmp_path, monkeypatch):
    monkeypatch.setenv("TMUXBOT_STATE_DIR", str(tmp_path / "state"))
    cwd = tmp_path / "project"
    cwd.mkdir()
    transcript = tmp_path / "session.jsonl"
    write_session(transcript, cwd, "different-session")
    target = "project:0.0"
    payload = {
        "version": 1,
        "tmuxTarget": target,
        "cwd": str(cwd),
        "sessionId": "session-1",
        "transcriptPath": str(transcript),
        "state": "terminal_error",
        "observedAt": "2026-08-12T00:00:00.000Z",
        "error": {"message": "failed"},
    }
    actual = tmp_path / "actual.json"
    actual.write_text(json.dumps(payload), encoding="utf-8")
    record = pi_session_health.session_health_record_path(target)
    record.parent.mkdir(parents=True)
    record.symlink_to(actual)

    assert pi_session_health.read_session_health(target, cwd) is None

    record.unlink()
    record.write_text(json.dumps(payload), encoding="utf-8")
    assert pi_session_health.read_session_health(target, cwd) is None
