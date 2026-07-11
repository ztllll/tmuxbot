import sqlite3
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from datetime import datetime, timedelta, timezone
from threading import Barrier, Event

import pytest

from tmuxbot.control_plane import repository as repository_module
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
    conflicting_duplicate = RunEvent(
        event_id="evt-1",
        event_type="session.reclassified",
        aggregate_type="session",
        aggregate_id="alpha:0.0",
        payload={"classification": "managed"},
        occurred_at=event.occurred_at,
    )
    assert repo.append_event(conflicting_duplicate) is False
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


def test_repository_sets_setting_if_absent_atomically(tmp_path):
    path = tmp_path / "control.sqlite3"
    first = ControlPlaneRepository(path)
    second = ControlPlaneRepository(path)
    first.migrate()
    ready = Barrier(2)

    def insert(repo, value):
        ready.wait(timeout=5)
        return repo.set_setting_if_absent("auth.cookie_key", value)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(insert, (first, second), ("first", "second")))

    assert sorted(results) == [False, True]
    assert first.get_setting("auth.cookie_key") in {"first", "second"}
    assert first.get_setting("auth.cookie_key") == second.get_setting("auth.cookie_key")


def test_repository_rolls_back_failed_migration_and_can_retry(tmp_path, monkeypatch):
    path = tmp_path / "control.sqlite3"
    repo = ControlPlaneRepository(path)
    monkeypatch.setattr(
        repository_module,
        "MIGRATIONS",
        ((1, "CREATE TABLE partial(value TEXT); INSERT INTO missing(value) VALUES ('x');"),),
    )

    with pytest.raises(sqlite3.OperationalError, match="no such table: missing"):
        repo.migrate()

    with sqlite3.connect(path) as db:
        tables = {
            row[0]
            for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    assert "partial" not in tables
    assert "schema_migrations" not in tables

    monkeypatch.setattr(
        repository_module,
        "MIGRATIONS",
        ((1, "CREATE TABLE partial(value TEXT);"),),
    )
    repo.migrate()

    with sqlite3.connect(path) as db:
        assert db.execute("SELECT version FROM schema_migrations").fetchall() == [(1,)]
        assert db.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'partial'"
        ).fetchone() == ("partial",)


def test_repository_serializes_concurrent_migrations(tmp_path, monkeypatch):
    path = tmp_path / "control.sqlite3"
    migration_paused = Event()
    release_migration = Event()
    monkeypatch.setattr(
        repository_module,
        "MIGRATIONS",
        (
            (
                1,
                "CREATE TABLE concurrent_migration(value TEXT); SELECT hold_migration();",
            ),
        ),
    )

    class PausingRepository(ControlPlaneRepository):
        def _connect(self):
            connection = super()._connect()

            def hold_migration():
                migration_paused.set()
                if not release_migration.wait(timeout=5):
                    raise RuntimeError("timed out waiting to release migration")
                return 1

            connection.create_function("hold_migration", 0, hold_migration)
            return connection

    first = PausingRepository(path)
    second = PausingRepository(path)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first_result = pool.submit(first.migrate)
        assert migration_paused.wait(timeout=5)
        second_result = pool.submit(second.migrate)
        try:
            with pytest.raises(TimeoutError):
                second_result.result(timeout=0.2)
        finally:
            release_migration.set()
        first_result.result(timeout=5)
        second_result.result(timeout=5)

    with sqlite3.connect(path) as db:
        assert db.execute("SELECT version FROM schema_migrations").fetchall() == [(1,)]


def test_repository_upgrades_from_an_older_schema_version(tmp_path, monkeypatch):
    path = tmp_path / "control.sqlite3"
    repo = ControlPlaneRepository(path)
    migration_1 = (1, "CREATE TABLE legacy_records(value TEXT NOT NULL);")
    migration_2 = (2, "CREATE TABLE current_records(value TEXT NOT NULL);")
    monkeypatch.setattr(repository_module, "MIGRATIONS", (migration_1,))
    repo.migrate()
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO legacy_records(value) VALUES ('preserved')")

    monkeypatch.setattr(repository_module, "MIGRATIONS", (migration_1, migration_2))
    repo.migrate()

    with sqlite3.connect(path) as db:
        assert db.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall() == [
            (1,),
            (2,),
        ]
        assert db.execute("SELECT value FROM legacy_records").fetchone() == ("preserved",)
        assert db.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'current_records'"
        ).fetchone() == ("current_records",)


@pytest.mark.parametrize("versions", [(1, 1), (2, 1)])
def test_repository_rejects_migration_versions_that_are_not_strictly_increasing(
    tmp_path, monkeypatch, versions
):
    monkeypatch.setattr(
        repository_module,
        "MIGRATIONS",
        tuple((version, "SELECT 1;") for version in versions),
    )

    with pytest.raises(ValueError, match="strictly increasing"):
        ControlPlaneRepository(tmp_path / "control.sqlite3").migrate()


def test_repository_rejects_database_schema_newer_than_supported(tmp_path):
    path = tmp_path / "control.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE schema_migrations "
            "(version INTEGER PRIMARY KEY, applied_at INTEGER NOT NULL)"
        )
        db.execute("INSERT INTO schema_migrations(version, applied_at) VALUES (999, 0)")

    with pytest.raises(RuntimeError, match="newer than supported.*999"):
        ControlPlaneRepository(path).migrate()


def test_repository_does_not_ignore_non_event_id_constraint_errors(tmp_path):
    repo = ControlPlaneRepository(tmp_path / "control.sqlite3")
    repo.migrate()
    invalid = RunEvent(
        event_id="evt-invalid",
        event_type=None,  # type: ignore[arg-type]
        aggregate_type="session",
        aggregate_id="alpha:0.0",
        payload={},
        occurred_at=datetime(2026, 7, 11, tzinfo=timezone.utc),
    )

    with pytest.raises(sqlite3.IntegrityError, match="NOT NULL constraint failed"):
        repo.append_event(invalid)
