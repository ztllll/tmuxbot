import asyncio
from pathlib import Path
from types import SimpleNamespace

from tmuxbot.cli_idle import CliActivity, CliObservation, reconcile_cli_idle_once
from tmuxbot.state import Binding


class PiBackend:
    name = "pi"

    def __init__(self):
        self.hibernate_calls = []

    async def hibernate(self, item):
        self.hibernate_calls.append(item.name)
        return True


class Frontend:
    def __init__(self, item, backend):
        self.bindings = [item]
        self.backend = backend

    def backend_for(self, _item):
        return self.backend


class Repository:
    def active_teamrun_tmux_targets(self):
        return set()


def binding(timeout=None):
    return Binding(
        name="pi-route",
        chat_id=1,
        thread_id=None,
        tmux_session="pi-route",
        tmux_window=0,
        tmux_pane=0,
        cwd=Path("/tmp"),
        backend="pi",
        cli_idle_timeout_seconds=timeout,
    )


def state():
    return SimpleNamespace(
        cli_idle_since={"pi-route": 1.0},
        ensure_locks={},
        pending_rename={},
        command_transactions={},
        tui_fp={},
    )


def run(item):
    backend = PiBackend()
    runtime = state()
    asyncio.run(
        reconcile_cli_idle_once(
            [Frontend(item, backend)],
            runtime,
            Repository(),
            timeout=3600,
            now=7200,
            observer=lambda *_args: CliObservation(CliActivity.IDLE),
        )
    )
    return backend, runtime


def test_pi_inheriting_global_idle_timeout_stays_resident_as_extension_host():
    backend, runtime = run(binding(None))
    assert backend.hibernate_calls == []
    assert runtime.cli_idle_since == {}


def test_pi_can_explicitly_opt_in_to_idle_hibernation():
    backend, _runtime = run(binding(60))
    assert backend.hibernate_calls == ["pi-route"]


def test_pi_explicit_zero_remains_resident():
    backend, runtime = run(binding(0))
    assert backend.hibernate_calls == []
    assert runtime.cli_idle_since == {}
