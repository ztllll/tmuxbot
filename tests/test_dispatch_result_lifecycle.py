import asyncio
from pathlib import Path
from types import SimpleNamespace

from tmuxbot.dispatch import dispatch_incoming_text
from tmuxbot.state import Binding


def test_new_user_turn_clears_result_draft_and_duplicate_guard(monkeypatch, tmp_path: Path):
    sent = []

    async def fake_ensure(*_args, **_kwargs):
        return None

    async def fake_send(*args, **kwargs):
        sent.append((args, kwargs))

    monkeypatch.setattr("tmuxbot.dispatch.ensure_binding_running", fake_ensure)
    monkeypatch.setattr("tmuxbot.dispatch.tmux_send_text", fake_send)
    monkeypatch.setattr("tmuxbot.dispatch.tmux_has_session", lambda _name: True)
    monkeypatch.setattr("tmuxbot.dispatch.tmux_pane_command", lambda _target: "pi")

    binding = Binding(
        name="alpha",
        chat_id=123,
        thread_id=None,
        tmux_session="alpha",
        tmux_window=0,
        tmux_pane=0,
        cwd=tmp_path,
        backend="pi",
    )
    state = SimpleNamespace(
        pending_rename={},
        published_results={"alpha": "old final"},
        result_drafts={"alpha": "old draft"},
    )
    backend = SimpleNamespace(
        name="pi",
        running_command_names=frozenset({"pi"}),
        accepts_input_while_busy=True,
        is_running_command=lambda command: command == "pi",
        command_aliases=lambda: {},
        command_opts=lambda: {},
    )

    asyncio.run(
        dispatch_incoming_text(
            SimpleNamespace(), backend, binding, state, 123, None, "继续检查"
        )
    )

    assert state.published_results == {}
    assert state.result_drafts == {}
    assert len(sent) == 1
