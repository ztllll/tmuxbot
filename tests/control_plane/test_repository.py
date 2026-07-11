import sqlite3
from datetime import datetime, timedelta, timezone

from tmuxbot.control_plane.models import RunEvent
from tmuxbot.control_plane.repository import ControlPlaneRepository


def _event(event_id: str, occurred_at: datetime) -> RunEvent:
    return RunEvent(
        event_id=event_id,
        event_type="session.discovered",
        aggregate_type="session",
        aggregate_id="alpha:0.0",
        payload={"classification": "orphan", "metadata": {"label": "孤儿"}},
        occurred_at=occurred_at,
    )


def test_repository_migrates_repeatedly_and_appends_event_idempotently(tmp_path):
    path = tmp_path / "control.sqlite3"
    repo = ControlPlaneRepository(path)
    repo.migrate()
    repo.migrate()
    event = _event(
        "evt-1",
        datetime(2026, 7, 11, 8, 30, tzinfo=timezone(timedelta(hours=8))),
    )

    assert repo.append_event(event) is True
    assert repo.append_event(event) is False
    stored = repo.list_events(after_sequence=0, limit=10)

    assert len(stored) == 1
    assert stored[0].sequence == 1
    assert stored[0].payload == {
        "classification": "orphan",
        "metadata": {"label": "孤儿"},
    }
    assert stored[0].occurred_at == event.occurred_at
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM schema_migrations").fetchone() == (1,)


def test_repository_lists_events_after_sequence_in_sequence_order(tmp_path):
    repo = ControlPlaneRepository(tmp_path / "control.sqlite3")
    repo.migrate()
    occurred_at = datetime(2026, 7, 11, tzinfo=timezone.utc)
    for event_id in ("evt-1", "evt-2", "evt-3"):
        assert repo.append_event(_event(event_id, occurred_at)) is True

    stored = repo.list_events(after_sequence=1, limit=10)

    assert [(event.event_id, event.sequence) for event in stored] == [
        ("evt-2", 2),
        ("evt-3", 3),
    ]


def test_repository_persists_settings_and_web_sessions(tmp_path):
    repo = ControlPlaneRepository(tmp_path / "control.sqlite3")
    repo.migrate()
    repo.set_setting("auth.password_hash", "argon2-value")
    repo.set_setting("auth.password_hash", "argon2-updated")
    repo.create_session("token-hash", "csrf-value", expires_at=2_000_000_000)

    assert repo.get_setting("auth.password_hash") == "argon2-updated"
    assert repo.get_setting("missing") is None
    assert repo.get_session("token-hash", now=1_900_000_000) == "csrf-value"
    assert repo.get_session("token-hash", now=2_000_000_000) is None
    assert repo.get_session("token-hash", now=2_100_000_000) is None

    repo.delete_session("token-hash")

    assert repo.get_session("token-hash", now=1_900_000_000) is None
