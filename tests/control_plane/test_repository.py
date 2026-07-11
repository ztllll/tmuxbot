import os
import sqlite3
from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier, Event

import pytest

from tmuxbot.control_plane import repository as repository_module
from tmuxbot.control_plane.models import (
    ManagedSession,
    ProjectRecord,
    ProviderProfile,
    ProviderProbeResult,
    RunEvent,
)
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


def _mode(path):
    return path.stat().st_mode & 0o777


def test_repository_creates_private_data_directory_and_database_with_permissive_umask(
    tmp_path,
):
    data_dir = tmp_path / "data"
    path = data_dir / "control.sqlite3"
    previous_umask = os.umask(0o002)
    try:
        ControlPlaneRepository(path).migrate()
    finally:
        os.umask(previous_umask)

    assert _mode(data_dir) == 0o700
    assert _mode(path) == 0o600


@pytest.mark.parametrize("initial_mode", [0o644, 0o664])
def test_repository_repairs_existing_database_permissions(tmp_path, initial_mode):
    path = tmp_path / "control.sqlite3"
    path.touch(mode=initial_mode)
    path.chmod(initial_mode)

    ControlPlaneRepository(path).migrate()

    assert _mode(path) == 0o600


def test_repository_repairs_existing_data_directory_permissions(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir(mode=0o755)
    data_dir.chmod(0o755)

    ControlPlaneRepository(data_dir / "control.sqlite3").migrate()

    assert _mode(data_dir) == 0o700


def test_repository_secures_wal_and_shm_files_while_connection_is_open(tmp_path):
    path = tmp_path / "control.sqlite3"
    observed_modes = {}

    class InspectingRepository(ControlPlaneRepository):
        def _connect(self):
            connection = super()._connect()
            for suffix in ("-wal", "-shm"):
                sidecar = path.with_name(path.name + suffix)
                assert sidecar.exists()
                observed_modes[suffix] = _mode(sidecar)
            return connection

    with sqlite3.connect(path) as keeper:
        keeper.execute("PRAGMA journal_mode = WAL")
        keeper.execute("CREATE TABLE keep_sidecars(value TEXT)")
        keeper.execute("INSERT INTO keep_sidecars(value) VALUES ('open')")
        keeper.commit()
        for suffix in ("-wal", "-shm"):
            sidecar = path.with_name(path.name + suffix)
            assert sidecar.exists()
            sidecar.chmod(0o664)

        InspectingRepository(path).migrate()

    assert observed_modes == {"-wal": 0o600, "-shm": 0o600}


def test_repository_raises_clear_error_when_permissions_cannot_be_secured(
    tmp_path, monkeypatch
):
    path = tmp_path / "control.sqlite3"
    path.touch(mode=0o644)
    database_inode = path.stat().st_ino
    real_fchmod = os.fchmod

    def deny_database_chmod(descriptor, mode):
        if os.fstat(descriptor).st_ino == database_inode:
            raise PermissionError("operation not permitted")
        real_fchmod(descriptor, mode)

    monkeypatch.setattr(os, "fchmod", deny_database_chmod)

    with pytest.raises(RuntimeError, match="secure SQLite storage permissions.*control.sqlite3"):
        ControlPlaneRepository(path).migrate()


def test_repository_rejects_symlink_data_directory(tmp_path):
    actual_data_dir = tmp_path / "actual-data"
    actual_data_dir.mkdir()
    data_dir = tmp_path / "data"
    data_dir.symlink_to(actual_data_dir, target_is_directory=True)

    with pytest.raises(RuntimeError, match="data directory must not be a symbolic link"):
        ControlPlaneRepository(data_dir / "control.sqlite3").migrate()


def test_repository_rejects_symlink_in_existing_ancestor(tmp_path):
    actual_root = tmp_path / "actual-root"
    data_dir = actual_root / "data"
    data_dir.mkdir(parents=True)
    linked_root = tmp_path / "linked-root"
    linked_root.symlink_to(actual_root, target_is_directory=True)

    with pytest.raises(RuntimeError, match="ancestor must not be a symbolic link.*linked-root"):
        ControlPlaneRepository(linked_root / "data" / "control.sqlite3").migrate()


def test_repository_rejects_symlink_database_without_modifying_target(tmp_path):
    target = tmp_path / "target.sqlite3"
    target.write_bytes(b"not a database")
    target.chmod(0o644)
    path = tmp_path / "control.sqlite3"
    path.symlink_to(target)

    with pytest.raises(RuntimeError, match="refusing symbolic link.*control.sqlite3"):
        ControlPlaneRepository(path).migrate()

    assert target.read_bytes() == b"not a database"
    assert _mode(target) == 0o644


def test_repository_rejects_symlink_sidecar_without_modifying_target(tmp_path):
    path = tmp_path / "control.sqlite3"
    target = tmp_path / "sidecar-target"
    target.write_text("do not touch")
    target.chmod(0o644)
    path.with_name(path.name + "-wal").symlink_to(target)

    with pytest.raises(RuntimeError, match="refusing symbolic link.*-wal"):
        ControlPlaneRepository(path).migrate()

    assert target.read_text() == "do not touch"
    assert _mode(target) == 0o644


def test_repository_rejects_non_regular_sidecar(tmp_path):
    path = tmp_path / "control.sqlite3"
    path.with_name(path.name + "-shm").mkdir()

    with pytest.raises(RuntimeError, match="not a regular file.*-shm"):
        ControlPlaneRepository(path).migrate()


def test_repository_rejects_fifo_sidecar_without_blocking(tmp_path):
    path = tmp_path / "control.sqlite3"
    fifo = path.with_name(path.name + "-wal")
    os.mkfifo(fifo)

    with ThreadPoolExecutor(max_workers=1) as pool:
        result = pool.submit(ControlPlaneRepository(path).migrate)
        try:
            with pytest.raises(RuntimeError, match="not a regular file.*-wal"):
                result.result(timeout=0.5)
        except FutureTimeoutError:
            writer = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
            os.close(writer)
            with pytest.raises(RuntimeError):
                result.result(timeout=1)
            pytest.fail("repository blocked opening a FIFO sidecar")


def test_repository_ignores_sidecar_deleted_before_secure_open(tmp_path, monkeypatch):
    path = tmp_path / "control.sqlite3"
    real_open = os.open
    raced_sidecars = []

    def delete_sidecars_before_open(target, flags, mode=0o777):
        if os.fspath(target).endswith(("-wal", "-shm")):
            raced_sidecars.append(os.fspath(target))
            raise FileNotFoundError(os.fspath(target))
        return real_open(target, flags, mode)

    monkeypatch.setattr(os, "open", delete_sidecars_before_open)

    ControlPlaneRepository(path).migrate()

    assert {Path(item).suffix for item in raced_sidecars} == {".sqlite3-wal", ".sqlite3-shm"}


def test_repository_rejects_database_replaced_before_sqlite_connect(tmp_path, monkeypatch):
    path = tmp_path / "control.sqlite3"
    displaced = tmp_path / "displaced.sqlite3"
    real_connect = sqlite3.connect
    replaced = False

    def replace_before_connect(target, *args, **kwargs):
        nonlocal replaced
        if not replaced and os.fspath(target) == os.fspath(path):
            replaced = True
            os.replace(path, displaced)
            path.touch(mode=0o600)
        return real_connect(target, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", replace_before_connect)

    with pytest.raises(RuntimeError, match="database path changed before SQLite connected"):
        ControlPlaneRepository(path).migrate()

    assert replaced is True


def test_repository_first_write_secures_live_sidecars_before_commit(tmp_path):
    path = tmp_path / "control.sqlite3"
    observed = {}

    class InspectingFirstWriteRepository(ControlPlaneRepository):
        active_connection = None

        def _connect(self):
            connection = super()._connect()
            self.active_connection = connection
            return connection

        def _secure_storage_permissions(self):
            super()._secure_storage_permissions()
            if self.active_connection is not None and self.active_connection.in_transaction:
                observed["in_transaction"] = True
                for suffix in ("-wal", "-shm"):
                    sidecar = path.with_name(path.name + suffix)
                    observed[suffix] = (sidecar.exists(), _mode(sidecar))

    InspectingFirstWriteRepository(path).migrate()

    assert observed == {
        "in_transaction": True,
        "-wal": (True, 0o600),
        "-shm": (True, 0o600),
    }


def test_repository_permission_failure_before_commit_rolls_back_write(tmp_path):
    path = tmp_path / "control.sqlite3"
    ControlPlaneRepository(path).migrate()

    class FailingBeforeCommitRepository(ControlPlaneRepository):
        active_connection = None

        def _connect(self):
            connection = super()._connect()
            self.active_connection = connection
            return connection

        def _secure_storage_permissions(self):
            if self.active_connection is not None and self.active_connection.in_transaction:
                raise RuntimeError("simulated chmod failure")
            super()._secure_storage_permissions()

    repo = FailingBeforeCommitRepository(path)

    with pytest.raises(RuntimeError, match="simulated chmod failure"):
        repo.set_setting("must.rollback", "unsafe")

    with sqlite3.connect(path) as db:
        assert db.execute("SELECT value FROM settings WHERE key = 'must.rollback'").fetchone() is None


def test_repository_preserves_original_sqlite_error_when_permission_check_would_fail(
    tmp_path, monkeypatch
):
    path = tmp_path / "control.sqlite3"
    repo = ControlPlaneRepository(path)
    secure_calls = 0
    real_secure = repo._secure_storage_permissions

    def fail_after_connect():
        nonlocal secure_calls
        secure_calls += 1
        if secure_calls >= 2:
            raise RuntimeError("must not mask sqlite error")
        real_secure()

    repo._secure_storage_permissions = fail_after_connect  # type: ignore[method-assign]
    monkeypatch.setattr(
        repository_module,
        "MIGRATIONS",
        ((1, "INSERT INTO missing(value) VALUES ('x');"),),
    )

    with pytest.raises(sqlite3.OperationalError, match="no such table: missing"):
        repo.migrate()


def test_repository_preserves_body_error_when_rollback_and_close_fail():
    class BrokenCleanupConnection:
        def rollback(self):
            raise RuntimeError("rollback failed")

        def close(self):
            raise RuntimeError("close failed")

    repo = ControlPlaneRepository(Path("unused.sqlite3"))
    repo._connect = lambda: BrokenCleanupConnection()  # type: ignore[method-assign]

    with pytest.raises(sqlite3.OperationalError, match="primary sqlite failure"):
        with repo._connection():
            raise sqlite3.OperationalError("primary sqlite failure")


def test_repository_reports_close_error_without_primary_failure():
    class CloseFailureConnection:
        def commit(self):
            return None

        def close(self):
            raise RuntimeError("close failed")

    repo = ControlPlaneRepository(Path("unused.sqlite3"))
    repo._connect = lambda: CloseFailureConnection()  # type: ignore[method-assign]
    repo._secure_storage_permissions = lambda: None  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="close failed"):
        with repo._connection():
            pass


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
        assert db.execute("SELECT COUNT(*) FROM schema_migrations").fetchone() == (
            len(repository_module.MIGRATIONS),
        )


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


def test_repository_upgrades_v1_database_with_provider_control_plane_tables(tmp_path):
    path = tmp_path / "control.sqlite3"
    with sqlite3.connect(path) as db:
        version_one_sql = repository_module.MIGRATIONS[0][1]
        for statement in repository_module._migration_statements(version_one_sql):
            db.execute(statement)
        db.execute(
            "CREATE TABLE schema_migrations "
            "(version INTEGER PRIMARY KEY, applied_at INTEGER NOT NULL)"
        )
        db.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (1, 1)"
        )

    ControlPlaneRepository(path).migrate()

    with sqlite3.connect(path) as db:
        versions = db.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()
        tables = {
            row[0]
            for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert versions == [(1,), (2,)]
    assert {
        "provider_profiles",
        "projects",
        "managed_sessions",
        "probe_results",
    } <= tables


def test_repository_provider_project_session_and_probe_crud(tmp_path):
    repo = ControlPlaneRepository(tmp_path / "control.sqlite3")
    repo.migrate()
    provider = ProviderProfile(
        id="provider-codex",
        binary_name="codex",
        executable_path="/opt/bin/codex",
        version=None,
        device=8,
        inode=101,
        mtime_ns=202,
        discovered_at=1_700_000_000,
    )
    project = ProjectRecord(
        id="project-alpha",
        name="Alpha",
        root_path="/srv/alpha",
        device=8,
        inode=303,
        mtime_ns=404,
        created_at=1_700_000_001,
    )
    session = ManagedSession(
        id="session-alpha-codex",
        project_id=project.id,
        provider_id=provider.id,
        name="Codex 审核",
        tmux_session="tmuxbot-alpha-codex",
        tmux_window=0,
        tmux_pane=0,
        status="ready",
        created_at=1_700_000_002,
    )
    probe = ProviderProbeResult(
        id="probe-one",
        provider_id=provider.id,
        success=True,
        version="codex 1.2.3",
        error_code=None,
        exit_code=0,
        duration_ms=25,
        output_truncated=False,
        observed_at=1_700_000_003,
    )

    assert repo.upsert_provider_profile(provider) == provider
    repo.update_provider_version(provider.id, "codex 1.2.3")
    repo.create_project(project)
    repo.create_managed_session(session)
    repo.record_probe_result(probe)

    [stored_provider] = repo.list_provider_profiles()
    assert stored_provider.version == "codex 1.2.3"
    assert repo.get_provider_profile(provider.id) == stored_provider
    assert repo.list_projects() == [project]
    assert repo.list_managed_sessions() == [session]
    assert repo.list_probe_results(provider.id) == [probe]

    renamed_project = replace(project, name="Alpha Renamed", mtime_ns=405)
    renamed_session = replace(session, name="Codex Reviewer", status="stopped")
    assert repo.update_project(renamed_project) is True
    assert repo.update_managed_session(renamed_session) is True
    assert repo.get_project(project.id) == renamed_project
    assert repo.get_managed_session(session.id) == renamed_session

    assert repo.delete_managed_session(session.id) is True
    assert repo.delete_project(project.id) is True
    assert repo.delete_provider_profile(provider.id) is True
    assert repo.get_managed_session(session.id) is None
    assert repo.get_project(project.id) is None
    assert repo.get_provider_profile(provider.id) is None
    assert repo.list_probe_results(provider.id) == []


def test_repository_enforces_provider_and_path_uniqueness(tmp_path):
    repo = ControlPlaneRepository(tmp_path / "control.sqlite3")
    repo.migrate()
    first = ProviderProfile(
        id="provider-one",
        binary_name="claude",
        executable_path="/opt/bin/claude",
        version=None,
        device=1,
        inode=2,
        mtime_ns=3,
        discovered_at=4,
    )
    repo.upsert_provider_profile(first)

    duplicate = ProviderProfile(
        id="provider-two",
        binary_name="claude",
        executable_path="/opt/bin/claude",
        version=None,
        device=1,
        inode=2,
        mtime_ns=3,
        discovered_at=5,
    )

    assert repo.upsert_provider_profile(duplicate).id == first.id
    assert len(repo.list_provider_profiles()) == 1


def test_provider_rescan_preserves_version_only_for_unchanged_identity(tmp_path):
    repo = ControlPlaneRepository(tmp_path / "control.sqlite3")
    repo.migrate()
    original = ProviderProfile(
        id="provider-one",
        binary_name="codex",
        executable_path="/opt/bin/codex",
        version="codex 1",
        device=1,
        inode=2,
        mtime_ns=3,
        discovered_at=4,
    )
    repo.upsert_provider_profile(original)
    unchanged_scan = ProviderProfile(
        id="new-id-ignored",
        binary_name="codex",
        executable_path="/opt/bin/codex",
        version=None,
        device=1,
        inode=2,
        mtime_ns=3,
        discovered_at=5,
    )

    assert repo.upsert_provider_profile(unchanged_scan).version == "codex 1"

    changed_scan = ProviderProfile(
        id="another-id-ignored",
        binary_name="codex",
        executable_path="/opt/bin/codex",
        version=None,
        device=1,
        inode=99,
        mtime_ns=100,
        discovered_at=6,
    )
    assert repo.upsert_provider_profile(changed_scan).version is None


def test_provider_records_do_not_store_probe_output_or_secret_fields(tmp_path):
    repo = ControlPlaneRepository(tmp_path / "control.sqlite3")
    repo.migrate()

    with sqlite3.connect(repo.path) as db:
        provider_columns = {
            row[1] for row in db.execute("PRAGMA table_info(provider_profiles)")
        }
        probe_columns = {row[1] for row in db.execute("PRAGMA table_info(probe_results)")}

    forbidden = {"token", "secret", "password", "stdout", "stderr", "command", "argv"}
    assert provider_columns.isdisjoint(forbidden)
    assert probe_columns.isdisjoint(forbidden)


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
    with ThreadPoolExecutor(max_workers=1) as pool:
        first_result = pool.submit(first.migrate)
        assert migration_paused.wait(timeout=5)
        try:
            with closing(sqlite3.connect(path)) as contender:
                contender.execute("PRAGMA busy_timeout = 50")
                with pytest.raises(sqlite3.OperationalError, match="database is locked"):
                    contender.execute("BEGIN IMMEDIATE")
        finally:
            release_migration.set()
        first_result.result(timeout=5)

    ControlPlaneRepository(path).migrate()

    with sqlite3.connect(path) as db:
        assert db.execute("SELECT version FROM schema_migrations").fetchall() == [(1,)]
        assert db.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' AND name = 'concurrent_migration'"
        ).fetchone() == ("concurrent_migration",)


def test_repository_ignores_trailing_migration_comments(tmp_path, monkeypatch):
    path = tmp_path / "control.sqlite3"
    monkeypatch.setattr(
        repository_module,
        "MIGRATIONS",
        ((1, "CREATE TABLE comment_tail(value TEXT); -- trailing migration comment"),),
    )

    ControlPlaneRepository(path).migrate()

    with sqlite3.connect(path) as db:
        assert db.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'comment_tail'"
        ).fetchone() == ("comment_tail",)


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
