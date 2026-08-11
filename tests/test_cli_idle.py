import asyncio
from pathlib import Path
from types import SimpleNamespace

from tmuxbot.cli_idle import (
    CliActivity,
    CliObservation,
    reconcile_cli_idle_once,
)
from tmuxbot.state import Binding


def binding(name="alpha"):
    return Binding(
        name=name,
        chat_id=1,
        thread_id=None,
        tmux_session=name,
        tmux_window=0,
        tmux_pane=0,
        cwd=Path("/tmp"),
        backend="pi",
    )


class Backend:
    name = "codex"

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
    def __init__(self, protected=()):
        self.protected = set(protected)

    def active_teamrun_tmux_targets(self):
        return set(self.protected)


def state():
    return SimpleNamespace(
        cli_idle_since={},
        ensure_locks={},
        pending_rename={},
        command_transactions={},
        tui_fp={},
    )


def test_working_cli_resets_idle_clock_without_using_im_activity():
    item = binding()
    backend = Backend()
    runtime_state = state()
    runtime_state.cli_idle_since[item.name] = 10.0

    asyncio.run(
        reconcile_cli_idle_once(
            [Frontend(item, backend)],
            runtime_state,
            Repository(),
            timeout=3600,
            now=4000,
            observer=lambda *_args: CliObservation(CliActivity.WORKING),
        )
    )

    assert runtime_state.cli_idle_since == {}
    assert backend.hibernate_calls == []


def test_continuously_idle_cli_hibernates_but_keeps_tmux_route():
    item = binding()
    backend = Backend()
    frontend = Frontend(item, backend)
    runtime_state = state()
    observations = iter(
        [
            CliObservation(CliActivity.IDLE),
            CliObservation(CliActivity.IDLE),
            CliObservation(CliActivity.IDLE),
        ]
    )

    asyncio.run(
        reconcile_cli_idle_once(
            [frontend],
            runtime_state,
            Repository(),
            timeout=3600,
            now=100,
            observer=lambda *_args: next(observations),
        )
    )
    asyncio.run(
        reconcile_cli_idle_once(
            [frontend],
            runtime_state,
            Repository(),
            timeout=3600,
            now=3701,
            observer=lambda *_args: next(observations),
        )
    )

    assert backend.hibernate_calls == ["alpha"]
    assert frontend.bindings == [item]
    assert runtime_state.cli_idle_since == {}


def test_missing_tmux_and_shell_are_never_recreated_by_idle_reaper():
    for activity in (CliActivity.ABSENT, CliActivity.SHELL):
        item = binding(activity.value)
        backend = Backend()
        runtime_state = state()

        asyncio.run(
            reconcile_cli_idle_once(
                [Frontend(item, backend)],
                runtime_state,
                Repository(),
                timeout=1,
                now=100,
                observer=lambda *_args, value=activity: CliObservation(value),
            )
        )

        assert backend.hibernate_calls == []
        assert runtime_state.cli_idle_since == {}


def test_draft_interaction_and_active_teamrun_block_hibernation():
    blocked = (
        CliActivity.DRAFT,
        CliActivity.INTERACTION,
        CliActivity.WAITING,
        CliActivity.UNKNOWN,
    )
    for activity in blocked:
        item = binding(activity.value)
        backend = Backend()
        runtime_state = state()
        runtime_state.cli_idle_since[item.name] = 1

        asyncio.run(
            reconcile_cli_idle_once(
                [Frontend(item, backend)],
                runtime_state,
                Repository(),
                timeout=1,
                now=100,
                observer=lambda *_args, value=activity: CliObservation(value),
            )
        )
        assert backend.hibernate_calls == []

    item = binding("teamrun")
    backend = Backend()
    runtime_state = state()
    runtime_state.cli_idle_since[item.name] = 1
    asyncio.run(
        reconcile_cli_idle_once(
            [Frontend(item, backend)],
            runtime_state,
            Repository({item.tmux_target}),
            timeout=1,
            now=100,
            observer=lambda *_args: CliObservation(CliActivity.IDLE),
        )
    )
    assert backend.hibernate_calls == []


def test_command_rename_and_session_handoff_transactions_block_hibernation():
    blockers = ("command_transactions", "pending_rename")
    for blocker in blockers:
        item = binding(blocker)
        backend = Backend()
        runtime_state = state()
        runtime_state.cli_idle_since[item.name] = 1
        getattr(runtime_state, blocker)[item.name] = object()

        asyncio.run(
            reconcile_cli_idle_once(
                [Frontend(item, backend)],
                runtime_state,
                Repository(),
                timeout=1,
                now=100,
                observer=lambda *_args: CliObservation(CliActivity.IDLE),
            )
        )
        assert backend.hibernate_calls == []

    item = binding("handoff")
    item.pending_session_handoff_after = 10
    backend = Backend()
    runtime_state = state()
    runtime_state.cli_idle_since[item.name] = 1
    asyncio.run(
        reconcile_cli_idle_once(
            [Frontend(item, backend)],
            runtime_state,
            Repository(),
            timeout=1,
            now=100,
            observer=lambda *_args: CliObservation(CliActivity.IDLE),
        )
    )
    assert backend.hibernate_calls == []


def test_timeout_zero_keeps_provider_cli_resident():
    item = binding("resident")
    backend = Backend()
    runtime_state = state()
    runtime_state.cli_idle_since[item.name] = 1

    asyncio.run(
        reconcile_cli_idle_once(
            [Frontend(item, backend)],
            runtime_state,
            Repository(),
            timeout=0,
            now=100,
            observer=lambda *_args: CliObservation(CliActivity.IDLE),
        )
    )

    assert backend.hibernate_calls == []
    assert runtime_state.cli_idle_since == {}


def test_idle_reaper_rechecks_under_same_lock_as_incoming_messages():
    async def scenario():
        item = binding()
        backend = Backend()
        runtime_state = state()
        runtime_state.cli_idle_since[item.name] = 1
        lock = asyncio.Lock()
        runtime_state.ensure_locks[item.name] = lock
        await lock.acquire()
        try:
            await reconcile_cli_idle_once(
                [Frontend(item, backend)],
                runtime_state,
                Repository(),
                timeout=1,
                now=100,
                observer=lambda *_args: CliObservation(CliActivity.IDLE),
            )
        finally:
            lock.release()
        assert backend.hibernate_calls == []

    asyncio.run(scenario())
