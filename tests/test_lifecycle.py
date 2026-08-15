import asyncio
from pathlib import Path
from types import SimpleNamespace

from tmuxbot.lifecycle import (
    DEFAULT_LIFECYCLE_INTERVAL,
    ensure_binding_running,
    lifecycle_enabled,
    lifecycle_watch_loop,
    restart_binding,
)
from tmuxbot.state import Binding


class FakeBackend:
    def __init__(self) -> None:
        self.calls = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def ensure_running(self, binding):
        self.calls += 1
        self.started.set()
        await self.release.wait()


def binding(name="alpha"):
    return Binding(
        name=name,
        chat_id=123,
        thread_id=None,
        tmux_session=f"{name}-session",
        tmux_window=0,
        tmux_pane=0,
        cwd=Path("/tmp/tmuxbot-alpha"),
    )


def test_lifecycle_health_audit_is_enabled_by_default(monkeypatch):
    monkeypatch.delenv("TMUXBOT_LIFECYCLE_ENABLED", raising=False)
    assert lifecycle_enabled() is True

    monkeypatch.setenv("TMUXBOT_LIFECYCLE_ENABLED", "0")
    assert lifecycle_enabled() is False


def test_disabled_health_audit_leaves_missing_bindings_dormant(monkeypatch):
    monkeypatch.setenv("TMUXBOT_LIFECYCLE_ENABLED", "0")
    backend = FakeBackend()
    frontend = SimpleNamespace(backend=backend, bindings=[binding()])

    asyncio.run(
        lifecycle_watch_loop(
            [frontend],
            SimpleNamespace(ensure_locks={}),
            startup_delay=0,
        )
    )

    assert backend.calls == 0


def test_health_audit_defaults_to_one_hour_and_skips_manually_closed_tmux_sessions(monkeypatch):
    async def run():
        monkeypatch.setenv("TMUXBOT_LIFECYCLE_ENABLED", "1")
        monkeypatch.setattr("tmuxbot.tmux.tmux_has_session", lambda _session: False)
        backend = FakeBackend()
        frontend = SimpleNamespace(bindings=[binding()], backend_for=lambda _binding: backend)
        task = asyncio.create_task(
            lifecycle_watch_loop([frontend], SimpleNamespace(ensure_locks={}), startup_delay=0)
        )
        await asyncio.sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        assert DEFAULT_LIFECYCLE_INTERVAL == 3600.0
        assert backend.calls == 0

    asyncio.run(run())


def test_health_audit_checks_existing_tmux_sessions(monkeypatch):
    class QuickBackend:
        def __init__(self):
            self.calls = 0

        async def ensure_running(self, _binding):
            self.calls += 1

    async def run():
        monkeypatch.setenv("TMUXBOT_LIFECYCLE_ENABLED", "1")
        monkeypatch.setattr("tmuxbot.tmux.tmux_has_session", lambda _session: True)
        backend = QuickBackend()
        frontend = SimpleNamespace(bindings=[binding()], backend_for=lambda _binding: backend)
        task = asyncio.create_task(
            lifecycle_watch_loop([frontend], SimpleNamespace(ensure_locks={}), startup_delay=0)
        )
        for _ in range(10):
            if backend.calls:
                break
            await asyncio.sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        assert backend.calls == 1

    asyncio.run(run())


def test_ensure_binding_running_skips_background_when_lock_is_busy():
    async def run():
        state = SimpleNamespace(ensure_locks={})
        backend = FakeBackend()
        b = binding()

        first = asyncio.create_task(
            ensure_binding_running(backend, b, state, reason="incoming", wait=True)
        )
        await backend.started.wait()

        skipped = await ensure_binding_running(backend, b, state, reason="watchdog", wait=False)

        backend.release.set()
        await first

        assert skipped is False
        assert backend.calls == 1

    asyncio.run(run())


def test_ensure_binding_running_waits_for_existing_lock_when_requested():
    async def run():
        state = SimpleNamespace(ensure_locks={})
        backend = FakeBackend()
        b = binding()

        first = asyncio.create_task(
            ensure_binding_running(backend, b, state, reason="incoming", wait=True)
        )
        await backend.started.wait()

        second = asyncio.create_task(
            ensure_binding_running(backend, b, state, reason="restart", wait=True)
        )
        await asyncio.sleep(0)
        assert backend.calls == 1

        backend.release.set()
        await asyncio.gather(first, second)

        assert backend.calls == 2

    asyncio.run(run())


def test_ensure_binding_running_recovers_only_after_provider_health_failure():
    class RecoveringBackend:
        def __init__(self) -> None:
            self.ensure_calls = 0
            self.recovery_calls = 0

        async def ensure_running(self, _binding):
            self.ensure_calls += 1
            if self.ensure_calls == 1:
                raise RuntimeError("stopped provider sibling")

        async def recover_unhealthy_pane(self, _binding):
            self.recovery_calls += 1
            await self.ensure_running(_binding)
            return True

    backend = RecoveringBackend()
    state = SimpleNamespace(ensure_locks={})

    assert (
        asyncio.run(ensure_binding_running(backend, binding(), state, reason="incoming", wait=True))
        is True
    )
    assert backend.ensure_calls == 2
    assert backend.recovery_calls == 1


def test_ensure_binding_running_preserves_bound_provider_identity(tmp_path):
    old = tmp_path / "old.jsonl"
    old.write_text("old")

    class RestartingBackend:
        async def ensure_running(self, binding):
            return None

    b = binding()
    b.provider_session_id = "old-session"
    b.last_session_id = "old-session"
    b.transcript_path = old
    state = SimpleNamespace(ensure_locks={})

    asyncio.run(ensure_binding_running(RestartingBackend(), b, state, reason="restart", wait=True))

    assert b.provider_session_id == "old-session"
    assert b.last_session_id == "old-session"
    assert b.transcript_path == old


def test_restart_binding_cleanly_respawns_omp_without_tui_keys(monkeypatch, tmp_path):
    calls = []

    class OmpBackend:
        restart_via_clean_respawn = True

        def is_running_command(self, command):
            return command == "omp"

        async def ensure_running(self, _binding):
            calls.append("ensure")

    async def no_sleep(_delay):
        return None

    monkeypatch.setattr("tmuxbot.tmux.tmux_has_session", lambda _session: True)
    monkeypatch.setattr("tmuxbot.tmux.tmux_pane_command", lambda _target: "omp")
    monkeypatch.setattr(
        "tmuxbot.tmux.tmux_respawn_pane",
        lambda target, cwd: calls.append(("respawn", target, cwd)) or True,
    )
    monkeypatch.setattr(
        "tmuxbot.tmux.tmux_send_key",
        lambda *_args: calls.append("key"),
    )
    monkeypatch.setattr("tmuxbot.lifecycle.asyncio.sleep", no_sleep)

    b = binding("omp-route")
    transcript = tmp_path / "session.jsonl"
    b.provider_session_id = "session-id"
    b.transcript_path = transcript

    was_running = asyncio.run(
        restart_binding(
            OmpBackend(), b, SimpleNamespace(ensure_locks={}), reason="operator-restart"
        )
    )

    assert was_running is True
    assert calls == [("respawn", b.tmux_target, b.cwd), "ensure"]
    assert b.provider_session_id == "session-id"
    assert b.transcript_path == transcript


def test_restart_binding_fresh_respawn_drops_provider_identity(monkeypatch, tmp_path):
    calls = []

    class OmpBackend:
        restart_via_clean_respawn = True

        def is_running_command(self, command):
            return command == "omp"

        async def ensure_running(self, route):
            calls.append("ensure")
            assert route.provider_session_id is None
            assert route.last_session_id is None
            assert route.transcript_path is None
            assert route.fresh_start_pending is True

    async def no_sleep(_delay):
        return None

    monkeypatch.setattr("tmuxbot.tmux.tmux_has_session", lambda _session: True)
    monkeypatch.setattr("tmuxbot.tmux.tmux_pane_command", lambda _target: "omp")
    monkeypatch.setattr(
        "tmuxbot.tmux.tmux_respawn_pane",
        lambda target, cwd: calls.append(("respawn", target, cwd)) or True,
    )
    monkeypatch.setattr("tmuxbot.lifecycle.asyncio.sleep", no_sleep)

    b = binding("omp-route")
    b.provider_session_id = "old-session"
    b.last_session_id = "old-session"
    b.transcript_path = tmp_path / "old.jsonl"

    was_running = asyncio.run(
        restart_binding(
            OmpBackend(),
            b,
            SimpleNamespace(ensure_locks={}),
            reason="channel-exit-clean-reload",
            fresh=True,
            delay=3.0,
        )
    )

    assert was_running is True
    assert calls == [("respawn", b.tmux_target, b.cwd), "ensure"]
    assert b.provider_session_id is None
    assert b.last_session_id is None
    assert b.transcript_path is None
