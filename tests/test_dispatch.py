import asyncio
from pathlib import Path
from types import SimpleNamespace

from tmuxbot.backends.base import CmdOpts
from tmuxbot.dispatch import dispatch_incoming_text
from tmuxbot.state import Binding


class _Backend:
    name = "codex"
    running_command_names = frozenset({"codex"})

    def command_aliases(self):
        return {}

    def command_opts(self):
        return {"/new": CmdOpts(expect_new_session=True)}


def _binding() -> Binding:
    return Binding(
        name="handoff",
        chat_id=1,
        thread_id=None,
        tmux_session="handoff",
        tmux_window=0,
        tmux_pane=0,
        cwd=Path("/tmp/handoff"),
        backend="codex",
        provider_session_id="old-session",
    )


def test_channel_new_arms_session_handoff_before_tmux_injection(monkeypatch):
    binding = _binding()
    sent = []

    async def ready(*_args, **_kwargs):
        return True

    async def send_text(*args, **_kwargs):
        sent.append(args)

    def fire(coro):
        coro.close()

    monkeypatch.setattr("tmuxbot.dispatch.ensure_binding_running", ready)
    monkeypatch.setattr("tmuxbot.dispatch.tmux_send_text", send_text)

    asyncio.run(
        dispatch_incoming_text(
            SimpleNamespace(),
            _Backend(),
            binding,
            SimpleNamespace(pending_rename={}, fire=fire),
            1,
            None,
            "/new",
        )
    )

    assert binding.pending_session_handoff_after is not None
    assert sent[0][1] == "/new"
