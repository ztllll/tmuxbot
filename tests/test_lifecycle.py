import asyncio
from pathlib import Path
from types import SimpleNamespace

from tmuxbot.lifecycle import ensure_binding_running
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


def test_ensure_binding_running_skips_background_when_lock_is_busy():
    async def run():
        state = SimpleNamespace(ensure_locks={})
        backend = FakeBackend()
        b = binding()

        first = asyncio.create_task(
            ensure_binding_running(backend, b, state, reason="incoming", wait=True)
        )
        await backend.started.wait()

        skipped = await ensure_binding_running(
            backend, b, state, reason="watchdog", wait=False
        )

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


def test_ensure_binding_running_rebinds_new_transcript_after_cli_restart(
    tmp_path, monkeypatch
):
    old = tmp_path / "old.jsonl"
    new = tmp_path / "new.jsonl"
    old.write_text("old")
    new.write_text("new")
    commands = iter(["bash", "node"])
    monkeypatch.setattr("tmuxbot.lifecycle.tmux_pane_command", lambda target: next(commands))

    class RestartingBackend:
        async def ensure_running(self, binding):
            return None

        def is_running_command(self, command):
            return command == "node"

        def find_active_jsonl(self, probe):
            assert probe.provider_session_id is None
            assert probe.transcript_path is None
            return new

        def session_identity(self, binding, transcript):
            return SimpleNamespace(
                session_id="new-session",
                transcript_path=str(transcript),
            )

    b = binding()
    b.provider_session_id = "old-session"
    b.last_session_id = "old-session"
    b.transcript_path = old
    state = SimpleNamespace(ensure_locks={})

    asyncio.run(
        ensure_binding_running(
            RestartingBackend(), b, state, reason="restart", wait=True
        )
    )

    assert b.provider_session_id == "new-session"
    assert b.last_session_id == "new-session"
    assert b.transcript_path == new
