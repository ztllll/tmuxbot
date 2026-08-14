import json
from pathlib import Path

import pytest

from tmuxbot.backends.omp import OmpBackend
from tmuxbot.runtime.omp_handoff import handoff_record_path, read_handoff
from tmuxbot.runtime.route_health import PaneProcess
from tmuxbot.state import Binding


@pytest.fixture(autouse=True)
def live_omp_process(monkeypatch):
    monkeypatch.setattr(
        "tmuxbot.runtime.route_health.pane_processes",
        lambda _target: (
            PaneProcess(
                pid=123,
                parent_pid=1,
                state="Sl+",
                command="/home/test/.local/bin/omp --approval-mode yolo",
            ),
        ),
    )


def route(cwd: Path, *, session: str = "project", pane: int = 0) -> Binding:
    return Binding(
        name=f"route-{pane}",
        chat_id=1,
        thread_id=None,
        tmux_session=session,
        tmux_window=0,
        tmux_pane=pane,
        cwd=cwd,
        backend="omp",
    )


def write_session(
    path: Path,
    cwd: Path,
    session_id: str,
    *,
    version: int = 3,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "not-json\n"
        + json.dumps({"type": "title", "title": "test"})
        + "\n"
        + json.dumps({"type": "session", "version": version, "id": session_id, "cwd": str(cwd)})
        + "\n",
        encoding="utf-8",
    )


def write_handoff(
    target: str,
    cwd: Path,
    session_id: str,
    transcript: Path,
    process_id: int | None = 123,
    version: int = 1,
) -> Path:
    path = handoff_record_path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "version": version,
                "tmuxTarget": target,
                "cwd": str(cwd),
                "sessionId": session_id,
                "transcriptPath": str(transcript),
                **({"processId": process_id} if process_id is not None else {}),
            }
        ),
        encoding="utf-8",
    )
    return path


def test_handoff_record_path_is_ascii_and_collision_resistant(tmp_path, monkeypatch):
    monkeypatch.setenv("TMUXBOT_STATE_DIR", str(tmp_path / "state"))

    chinese = handoff_record_path("omp-网络同传系统项目:0.0")
    colliding = handoff_record_path("omp-另一套同传系统项目:0.0")

    assert chinese.name.isascii()
    assert chinese != colliding
    assert chinese.suffix == ".json"


def test_read_handoff_rejects_legacy_filename_and_wrong_sidecar_version(tmp_path, monkeypatch):
    monkeypatch.setenv("TMUXBOT_STATE_DIR", str(tmp_path / "state"))
    cwd = tmp_path / "project"
    cwd.mkdir()
    transcript = tmp_path / "sessions" / "new.jsonl"
    write_session(transcript, cwd, "new")
    target = "project:0.0"
    legacy = handoff_record_path(target).with_name("project_0.0.json")
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text(
        json.dumps(
            {
                "version": 1,
                "tmuxTarget": target,
                "cwd": str(cwd),
                "sessionId": "new",
                "transcriptPath": str(transcript),
            }
        ),
        encoding="utf-8",
    )

    assert read_handoff(target, cwd) is None
    write_handoff(target, cwd, "new", transcript, version=2)
    assert read_handoff(target, cwd) is None


def test_read_handoff_validates_exact_target_cwd_header_version_and_update_time(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("TMUXBOT_STATE_DIR", str(tmp_path / "state"))
    cwd = tmp_path / "project"
    cwd.mkdir()
    transcript = tmp_path / "sessions" / "new.jsonl"
    write_session(transcript, cwd, "new")
    record = write_handoff("project:0.0", cwd, "new", transcript)

    handoff = read_handoff("project:0.0", cwd)

    assert handoff is not None
    assert handoff.session_id == "new"
    assert handoff.transcript_path == transcript
    assert handoff.updated_at == record.stat().st_mtime_ns / 1_000_000_000
    assert read_handoff("other:0.0", cwd) is None
    assert read_handoff("project:0.0", tmp_path / "other") is None

    for supported_version in (1, 2):
        write_session(transcript, cwd, "new", version=supported_version)
        assert read_handoff("project:0.0", cwd) is not None

    write_session(transcript, cwd, "new", version=4)
    assert read_handoff("project:0.0", cwd) is None


def test_read_handoff_rejects_sidecar_from_a_different_live_process(tmp_path, monkeypatch):
    monkeypatch.setenv("TMUXBOT_STATE_DIR", str(tmp_path / "state"))
    cwd = tmp_path / "project"
    cwd.mkdir()
    transcript = tmp_path / "sessions" / "new.jsonl"
    write_session(transcript, cwd, "new")
    write_handoff("project:0.0", cwd, "new", transcript, process_id=456)

    assert read_handoff("project:0.0", cwd) is None


def test_read_handoff_accepts_provider_identity_before_new_transcript_exists(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("TMUXBOT_STATE_DIR", str(tmp_path / "state"))
    cwd = tmp_path / "project"
    cwd.mkdir()
    transcript = (
        tmp_path
        / "home"
        / ".omp"
        / "agent"
        / "sessions"
        / "-tmp-project"
        / "2026-08-14T00-00-00-000Z_new-session.jsonl"
    )
    transcript.parent.mkdir(parents=True)
    target = "project:0.0"
    write_handoff(target, cwd, "new-session", transcript, process_id=123)
    monkeypatch.setattr(
        "tmuxbot.runtime.route_health.pane_processes",
        lambda _target: (
            __import__("tmuxbot.runtime.route_health", fromlist=["PaneProcess"]).PaneProcess(
                pid=123,
                parent_pid=1,
                state="Sl+",
                command="/home/test/.local/bin/omp --approval-mode yolo",
            ),
        ),
    )

    handoff = read_handoff(target, cwd)

    assert handoff is not None
    assert handoff.session_id == "new-session"
    assert handoff.transcript_path == transcript


def test_read_handoff_accepts_legacy_pi_identity_before_new_transcript_exists(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("TMUXBOT_STATE_DIR", str(tmp_path / "state"))
    cwd = tmp_path / "project"
    cwd.mkdir()
    transcript = (
        tmp_path
        / "home"
        / ".pi"
        / "agent"
        / "sessions"
        / "-tmp-project"
        / "2026-08-14T00-00-00-000Z_legacy-new-session.jsonl"
    )
    transcript.parent.mkdir(parents=True)
    target = "project:0.0"
    write_handoff(target, cwd, "legacy-new-session", transcript, process_id=123)

    handoff = read_handoff(target, cwd)

    assert handoff is not None
    assert handoff.session_id == "legacy-new-session"
    assert handoff.transcript_path == transcript


def test_read_handoff_rejects_symlink_sidecar_and_transcript(tmp_path, monkeypatch):
    monkeypatch.setenv("TMUXBOT_STATE_DIR", str(tmp_path / "state"))
    cwd = tmp_path / "project"
    cwd.mkdir()
    transcript = tmp_path / "session.jsonl"
    write_session(transcript, cwd, "session-1")
    target = "project:0.0"
    actual_record = tmp_path / "actual.json"
    record = write_handoff(target, cwd, "session-1", transcript)
    record.replace(actual_record)
    record.symlink_to(actual_record)
    assert read_handoff(target, cwd) is None

    record.unlink()
    actual_record.replace(record)
    actual_transcript = tmp_path / "actual.jsonl"
    transcript.replace(actual_transcript)
    transcript.symlink_to(actual_transcript)
    assert read_handoff(target, cwd) is None


def test_backend_prefers_exact_target_handoff_and_never_claims_same_cwd_other_pane(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("TMUXBOT_STATE_DIR", str(tmp_path / "state"))
    cwd = tmp_path / "project"
    cwd.mkdir()
    old = tmp_path / "old.jsonl"
    other = tmp_path / "other.jsonl"
    write_session(old, cwd, "old")
    write_session(other, cwd, "other")
    first = route(cwd, pane=0)
    first.provider_session_id = "old"
    first.transcript_path = old
    second = route(cwd, pane=1)
    write_handoff(second.tmux_target, cwd, "other", other)

    assert OmpBackend().find_active_jsonl(first) == old

    write_handoff(first.tmux_target, cwd, "other", other)
    assert OmpBackend().find_active_jsonl(first) == other


def test_backend_accepts_exact_legacy_pi_path_pin_without_moving_it(tmp_path, monkeypatch):
    monkeypatch.setenv("TMUXBOT_STATE_DIR", str(tmp_path / "state"))
    cwd = tmp_path / "project"
    cwd.mkdir()
    transcript = tmp_path / ".pi" / "agent" / "sessions" / "legacy.jsonl"
    write_session(transcript, cwd, "legacy")
    item = route(cwd)
    item.transcript_path = transcript

    assert OmpBackend().find_active_jsonl(item) == transcript
    assert transcript.is_file()
