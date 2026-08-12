import asyncio
from pathlib import Path
from types import SimpleNamespace

from tmuxbot.backends.base import CmdOpts
from tmuxbot.core.capabilities import ProviderCapabilities
from tmuxbot.core.events import TerminalState, TerminalStatus
from tmuxbot.dispatch import dispatch_incoming_text
from tmuxbot.state import Binding


class _Backend:
    name = "codex"
    running_command_names = frozenset({"codex"})

    def command_aliases(self):
        return {}

    @property
    def capabilities(self):
        return ProviderCapabilities(name=self.name)

    @property
    def accepts_input_while_busy(self):
        return self.capabilities.accepts_input_while_busy

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


def test_pi_new_while_working_fails_immediately_without_injection(monkeypatch):
    binding = _binding()
    binding.backend = "pi"
    calls = []

    class PiBackend(_Backend):
        name = "pi"
        running_command_names = frozenset({"pi"})

        @property
        def capabilities(self):
            return ProviderCapabilities(name=self.name, accepts_input_while_busy=True)

        def parse_terminal_status(self, _pane):
            return TerminalStatus(state=TerminalState.WORKING, label="Working...")

    class Frontend:
        async def send_html(self, chat_id, thread_id, text):
            calls.append(("reply", chat_id, thread_id, text))

    async def ready(*_args, **_kwargs):
        return True

    async def send_text(*_args, **_kwargs):
        calls.append(("inject",))

    monkeypatch.setattr("tmuxbot.dispatch.ensure_binding_running", ready)
    monkeypatch.setattr("tmuxbot.dispatch.tmux_capture", lambda *_args: "Working...")
    monkeypatch.setattr("tmuxbot.dispatch.tmux_send_text", send_text)

    asyncio.run(
        dispatch_incoming_text(
            Frontend(),
            PiBackend(),
            binding,
            SimpleNamespace(pending_rename={}),
            1,
            8024,
            "/new@ztlgpt_bot",
            bot_username="ztlgpt_bot",
        )
    )

    assert not any(call[0] == "inject" for call in calls)
    assert binding.pending_session_handoff_after is None
    assert len(calls) == 1
    assert "当前仍在 <code>Working" in calls[0][3]
    assert "/esc" in calls[0][3]
    assert "/new" in calls[0][3]


def test_pi_plan_command_while_working_fails_immediately_without_injection(monkeypatch):
    binding = _binding()
    binding.backend = "pi"
    calls = []

    class PiBackend(_Backend):
        name = "pi"
        running_command_names = frozenset({"pi"})

        @property
        def capabilities(self):
            return ProviderCapabilities(name=self.name, accepts_input_while_busy=True)

        def parse_terminal_status(self, _pane):
            return TerminalStatus(state=TerminalState.WORKING, label="Retrying (1/5)...")

    class Frontend:
        async def send_html(self, chat_id, thread_id, text):
            calls.append(("reply", chat_id, thread_id, text))

    async def ready(*_args, **_kwargs):
        return True

    async def send_text(*_args, **_kwargs):
        calls.append(("inject",))

    monkeypatch.setattr("tmuxbot.dispatch.ensure_binding_running", ready)
    monkeypatch.setattr("tmuxbot.dispatch.tmux_capture", lambda *_args: "Retrying")
    monkeypatch.setattr("tmuxbot.command_adapter.tmux_capture", lambda *_args: "Retrying")
    monkeypatch.setattr("tmuxbot.command_adapter.tmux_send_text", send_text)

    asyncio.run(
        dispatch_incoming_text(
            Frontend(),
            PiBackend(),
            binding,
            SimpleNamespace(pending_rename={}),
            1,
            8024,
            "/plan finalize",
        )
    )

    assert not any(call[0] == "inject" for call in calls)
    assert len(calls) == 1
    assert "/plan 未执行" in calls[0][3]
    assert "Retrying" in calls[0][3]
    assert "/esc" in calls[0][3]


def test_channel_clone_arms_session_handoff_before_tmux_injection(monkeypatch):
    binding = _binding()
    sent = []

    class Backend(_Backend):
        def command_opts(self):
            return {"/clone": CmdOpts(expect_session_handoff=True)}

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
            Backend(),
            binding,
            SimpleNamespace(pending_rename={}, fire=fire),
            1,
            None,
            "/clone",
        )
    )

    assert binding.pending_session_handoff_after is not None
    assert sent[0][1] == "/clone"


def test_stop_closes_tmux_without_starting_it_first(monkeypatch):
    b = _binding()
    calls = []

    async def ready(*_args, **_kwargs):
        calls.append("ensure")
        return True

    def stop(session):
        calls.append(("stop", session))
        return True

    class Frontend:
        async def send_html(self, chat_id, thread_id, text):
            calls.append(("reply", chat_id, thread_id, text))

    monkeypatch.setattr("tmuxbot.dispatch.ensure_binding_running", ready)
    monkeypatch.setattr("tmuxbot.dispatch.tmux_kill_session", stop)

    asyncio.run(
        dispatch_incoming_text(
            Frontend(),
            _Backend(),
            b,
            SimpleNamespace(pending_rename={}),
            1,
            None,
            "/tmuxstop",
        )
    )

    assert "ensure" not in calls
    assert ("stop", b.tmux_session) in calls
    assert any("下一条消息" in call[3] for call in calls if call[0] == "reply")


def test_slash_command_after_stop_starts_runtime_before_injection(monkeypatch):
    b = _binding()
    calls = []

    class Backend(_Backend):
        def command_opts(self):
            return {"/compact": CmdOpts()}

    async def ready(*_args, **_kwargs):
        calls.append("ensure")
        return True

    async def send_text(*args, **_kwargs):
        calls.append(("inject", args[1]))

    def fire(coro):
        coro.close()

    monkeypatch.setattr("tmuxbot.dispatch.ensure_binding_running", ready)
    monkeypatch.setattr("tmuxbot.dispatch.tmux_send_text", send_text)

    asyncio.run(
        dispatch_incoming_text(
            SimpleNamespace(),
            Backend(),
            b,
            SimpleNamespace(pending_rename={}, fire=fire),
            1,
            None,
            "/compact",
        )
    )

    assert calls == ["ensure", ("inject", "/compact")]


def test_restart_after_stop_only_starts_runtime(monkeypatch):
    b = _binding()
    calls = []

    async def ready(*_args, **_kwargs):
        calls.append("ensure")
        return True

    class Frontend:
        async def send_html(self, *_args):
            calls.append("reply")

    monkeypatch.setattr("tmuxbot.dispatch.ensure_binding_running", ready)
    monkeypatch.setattr("tmuxbot.dispatch.tmux_has_session", lambda _session: False)
    monkeypatch.setattr("tmuxbot.dispatch.tmux_send_key", lambda *_args: calls.append("key"))

    asyncio.run(
        dispatch_incoming_text(
            Frontend(), _Backend(), b, SimpleNamespace(pending_rename={}),
            1, None, "/restart",
        )
    )

    assert calls == ["ensure", "reply"]


def test_message_after_stop_starts_runtime_before_injection(monkeypatch):
    b = _binding()
    calls = []

    async def ready(*_args, **_kwargs):
        calls.append("ensure")
        return True

    async def send_text(*_args, **_kwargs):
        calls.append("inject")

    monkeypatch.setattr("tmuxbot.dispatch.ensure_binding_running", ready)
    monkeypatch.setattr("tmuxbot.dispatch.tmux_send_text", send_text)

    asyncio.run(
        dispatch_incoming_text(
            SimpleNamespace(),
            _Backend(),
            b,
            SimpleNamespace(pending_rename={}),
            1,
            None,
            "继续处理",
        )
    )

    assert calls == ["ensure", "inject"]


def test_pi_normal_message_enables_busy_submission(monkeypatch):
    b = _binding()
    b.backend = "pi"
    calls = []

    class PiBackend(_Backend):
        name = "pi"
        running_command_names = frozenset({"pi"})

        @property
        def capabilities(self):
            return ProviderCapabilities(name=self.name, accepts_input_while_busy=True)

    async def ready(*_args, **_kwargs):
        return True

    async def send_text(*args, **kwargs):
        calls.append((args, kwargs))

    monkeypatch.setattr("tmuxbot.dispatch.ensure_binding_running", ready)
    monkeypatch.setattr("tmuxbot.dispatch.tmux_send_text", send_text)

    asyncio.run(
        dispatch_incoming_text(
            SimpleNamespace(),
            PiBackend(),
            b,
            SimpleNamespace(pending_rename={}),
            1,
            None,
            "在当前工作后继续检查",
        )
    )

    assert calls[0][1]["allow_busy_submission"] is True


def test_pi_pending_rename_enables_busy_submission(monkeypatch):
    b = _binding()
    b.backend = "pi"
    calls = []

    class PiBackend(_Backend):
        name = "pi"
        running_command_names = frozenset({"pi"})

        @property
        def capabilities(self):
            return ProviderCapabilities(name=self.name, accepts_input_while_busy=True)

    async def ready(*_args, **_kwargs):
        return True

    async def send_text(*args, **kwargs):
        calls.append((args, kwargs))

    class Frontend:
        async def send_html(self, *_args, **_kwargs):
            return None

    monkeypatch.setattr("tmuxbot.dispatch.ensure_binding_running", ready)
    monkeypatch.setattr("tmuxbot.dispatch.tmux_send_text", send_text)

    asyncio.run(
        dispatch_incoming_text(
            Frontend(),
            PiBackend(),
            b,
            SimpleNamespace(pending_rename={b.name: __import__("time").time()}),
            1,
            None,
            "__pycache__/tmuxbot.cpython-310.",
        )
    )

    assert calls[0][1]["allow_busy_submission"] is True


def test_tmuxstop_reports_a_real_tmux_failure(monkeypatch):
    calls = []

    class Frontend:
        async def send_html(self, _chat_id, _thread_id, text):
            calls.append(text)

    monkeypatch.setattr("tmuxbot.dispatch.tmux_kill_session", lambda _session: False)

    asyncio.run(
        dispatch_incoming_text(
            Frontend(),
            _Backend(),
            _binding(),
            SimpleNamespace(pending_rename={}),
            1,
            None,
            "/tmuxstop",
        )
    )

    assert calls == ["❌ <b>tmux 会话关闭失败</b>\n请检查服务日志或在主机上手动关闭。"]
