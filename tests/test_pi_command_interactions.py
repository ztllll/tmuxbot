import asyncio
from pathlib import Path
from types import SimpleNamespace

from tmuxbot.command_adapter import (
    CommandKind,
    CommandSpec,
    CommandTransaction,
    handle_interactive_command,
    handle_tui_action,
)
from tmuxbot.state import Binding


def binding() -> Binding:
    return Binding(
        name="pi-route",
        chat_id=1,
        thread_id=None,
        tmux_session="pi-route",
        tmux_window=0,
        tmux_pane=0,
        cwd=Path("/tmp/pi-route"),
        backend="pi",
        provider_session_id="old-session",
        last_session_id="old-session",
    )


class Frontend:
    def __init__(self):
        self.html = []
        self.cards = []

    async def send_html(self, chat_id, thread_id, text):
        self.html.append((chat_id, thread_id, text))

    async def send_interaction_card(self, chat_id, thread_id, text, binding_name):
        self.cards.append((chat_id, thread_id, text, binding_name))


class Backend:
    name = "pi"
    running_command_names = frozenset({"pi"})
    accepts_input_while_busy = True

    def interactive_session_handoff_commands(self):
        return frozenset({"/resume"})

    async def reconcile_session_identity(self, item):
        item.provider_session_id = "new-session"
        item.last_session_id = "new-session"
        item.transcript_path = Path("/tmp/new-session.jsonl")
        item.pending_session_handoff_after = None
        return True


def transaction() -> CommandTransaction:
    return CommandTransaction(
        txn_id="txn",
        binding_name="pi-route",
        command="/resume",
        kind=CommandKind.INTERACTIVE,
        injected_text="/resume",
        started_at=123.0,
        start_session_id="old-session",
        start_pane_hash="old",
    )


def test_pi_resume_transaction_stays_open_until_selection(monkeypatch):
    item = binding()
    frontend = Frontend()
    state = SimpleNamespace(command_transactions={})

    async def send_text(*_args, **_kwargs):
        return None

    monkeypatch.setattr("tmuxbot.command_adapter.tmux_capture", lambda *_args: "Resume Session")
    monkeypatch.setattr("tmuxbot.command_adapter.tmux_send_text", send_text)

    asyncio.run(
        handle_interactive_command(
            frontend,
            Backend(),
            item,
            state,
            item.chat_id,
            item.thread_id,
            CommandSpec("/resume", CommandKind.INTERACTIVE),
            "/resume",
        )
    )

    assert item.name in state.command_transactions
    assert frontend.cards


def test_pi_resume_enter_reconciles_and_closes_transaction(monkeypatch):
    item = binding()
    frontend = Frontend()
    txn = transaction()
    state = SimpleNamespace(command_transactions={item.name: txn})
    sent_keys = []

    monkeypatch.setattr(
        "tmuxbot.command_adapter.tmux_send_key",
        lambda target, key: sent_keys.append((target, key)),
    )
    monkeypatch.setattr("tmuxbot.command_adapter.tmux_capture", lambda *_args: "Resumed session")

    asyncio.run(
        handle_tui_action(
            frontend,
            item,
            item.chat_id,
            item.thread_id,
            "enter",
            backend=Backend(),
            state=state,
        )
    )

    assert sent_keys == [(item.tmux_target, "Enter")]
    assert item.provider_session_id == "new-session"
    assert item.name not in state.command_transactions
    assert "会话切换已绑定" in frontend.html[-1][2]
