import asyncio
from pathlib import Path
from types import SimpleNamespace

from tmuxbot.backends.omp import OmpBackend

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
        name="omp-route",
        chat_id=1,
        thread_id=None,
        tmux_session="omp-route",
        tmux_window=0,
        tmux_pane=0,
        cwd=Path("/tmp/omp-route"),
        backend="omp",
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
    name = "omp"
    running_command_names = frozenset({"omp"})
    accepts_input_while_busy = True
    remote_tui_actions_allowed = False

    def format_remote_interaction_notice(self, item, interaction_label):
        return f"SSH {item.tmux_target} {interaction_label}"

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
        binding_name="omp-route",
        command="/resume",
        kind=CommandKind.INTERACTIVE,
        injected_text="/resume",
        started_at=123.0,
        start_session_id="old-session",
        start_pane_hash="old",
    )


def test_omp_resume_transaction_stays_open_for_ssh_selection(monkeypatch):
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
    assert frontend.cards == []
    assert "稍后出现交互界面" in frontend.html[-1][2]
    assert "SSH" in frontend.html[-1][2]


def test_omp_model_modal_returns_ssh_notice_without_remote_card(monkeypatch):
    item = binding()
    frontend = Frontend()
    state = SimpleNamespace(command_transactions={})
    fixture = (Path(__file__).parent / "fixtures" / "omp" / "model_picker.txt").read_text(
        encoding="utf-8"
    )

    async def send_text(*_args, **_kwargs):
        return None

    async def no_sleep(_delay):
        return None

    monkeypatch.setattr("tmuxbot.command_adapter.tmux_capture", lambda *_args: fixture)
    monkeypatch.setattr("tmuxbot.command_adapter.tmux_send_text", send_text)
    monkeypatch.setattr("tmuxbot.command_adapter.asyncio.sleep", no_sleep)

    asyncio.run(
        handle_interactive_command(
            frontend,
            OmpBackend(),
            item,
            state,
            item.chat_id,
            item.thread_id,
            CommandSpec("/model", CommandKind.INTERACTIVE),
            "/model",
        )
    )

    assert frontend.cards == []
    assert item.tmux_target in frontend.html[-1][2]
    assert "已确认当前 TUI 正停留在<b>选择菜单</b>" in frontend.html[-1][2]
    assert item.name not in state.command_transactions


def test_omp_resume_enter_is_rejected_without_identity_adoption(monkeypatch):
    item = binding()
    frontend = Frontend()
    txn = transaction()
    state = SimpleNamespace(command_transactions={item.name: txn})
    sent_keys = []

    monkeypatch.setattr(
        "tmuxbot.command_adapter.tmux_send_key",
        lambda target, key: sent_keys.append((target, key)),
    )

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

    assert sent_keys == []
    assert item.provider_session_id == "old-session"
    assert state.command_transactions[item.name] is txn
    assert f"SSH {item.tmux_target}" in frontend.html[-1][2]
