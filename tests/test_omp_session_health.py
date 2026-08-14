import json
from pathlib import Path

from tmuxbot.runtime import omp_session_health


def write_session(path: Path, cwd: Path, session_id: str, *, version: int = 3) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"type": "session", "version": version, "id": session_id, "cwd": str(cwd)})
        + "\n",
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
    target = "omp-中文:0.0"
    record = omp_session_health.session_health_record_path(target)
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

    health = omp_session_health.read_session_health(target, cwd)

    assert health is not None
    assert health.state == "terminal_error"
    assert health.session_id == "session-1"
    assert health.error_message == "503 unavailable"
    assert record.name.isascii()
    assert health.updated_at == record.stat().st_mtime_ns / 1_000_000_000


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
    record = omp_session_health.session_health_record_path(target)
    record.parent.mkdir(parents=True)
    record.symlink_to(actual)

    assert omp_session_health.read_session_health(target, cwd) is None

    record.unlink()
    record.write_text(json.dumps(payload), encoding="utf-8")
    assert omp_session_health.read_session_health(target, cwd) is None


def test_read_session_health_rejects_wrong_versions_relative_paths_and_bad_state(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("TMUXBOT_STATE_DIR", str(tmp_path / "state"))
    cwd = tmp_path / "project"
    cwd.mkdir()
    transcript = tmp_path / "session.jsonl"
    write_session(transcript, cwd, "session-1")
    target = "project:0.0"
    record = omp_session_health.session_health_record_path(target)
    record.parent.mkdir(parents=True)
    payload = {
        "version": 2,
        "tmuxTarget": target,
        "cwd": str(cwd),
        "sessionId": "session-1",
        "transcriptPath": str(transcript),
        "state": "idle",
        "observedAt": "2026-08-12T00:00:00.000Z",
    }
    record.write_text(json.dumps(payload), encoding="utf-8")
    assert omp_session_health.read_session_health(target, cwd) is None

    payload["version"] = 1
    payload["transcriptPath"] = "relative.jsonl"
    record.write_text(json.dumps(payload), encoding="utf-8")
    assert omp_session_health.read_session_health(target, cwd) is None

    payload["transcriptPath"] = str(transcript)
    payload["state"] = "settled"
    record.write_text(json.dumps(payload), encoding="utf-8")
    assert omp_session_health.read_session_health(target, cwd) is None

    payload["state"] = "idle"
    record.write_text(json.dumps(payload), encoding="utf-8")
    write_session(transcript, cwd, "session-1", version=4)
    assert omp_session_health.read_session_health(target, cwd) is None
