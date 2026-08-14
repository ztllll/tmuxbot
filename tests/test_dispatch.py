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

    def interactive_commands(self):
        return {}

    @property
    def remote_tui_actions_allowed(self):
        return self.name != "omp"

    @property
    def requires_idle_for_control_commands(self):
        return self.name == "omp"

    def format_remote_interaction_notice(self, binding, interaction_label):
        return f"SSH {binding.tmux_target} {interaction_label}"

    def format_remote_access_notice(self, binding, interaction_label):
        return f"SSH {binding.tmux_target} {interaction_label}"

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


def test_omp_new_while_working_fails_immediately_without_injection(monkeypatch):
    binding = _binding()
    binding.backend = "omp"
    calls = []

    class OmpBackend(_Backend):
        name = "omp"
        running_command_names = frozenset({"omp"})

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
            OmpBackend(),
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
    assert "/esc" not in calls[0][3]
    assert "/new" in calls[0][3]


def test_omp_plan_help_is_local_and_never_injected(monkeypatch):
    binding = _binding()
    binding.backend = "omp"
    calls = []

    class OmpBackend(_Backend):
        name = "omp"
        running_command_names = frozenset({"omp"})

        def read_plan_mode(self, _binding):
            return None

    class Frontend:
        async def send_html(self, chat_id, thread_id, text):
            calls.append(("reply", chat_id, thread_id, text))

    async def ready(*_args, **_kwargs):
        return True

    async def send_text(*args, **_kwargs):
        calls.append(("inject", args[1]))

    monkeypatch.setattr("tmuxbot.dispatch.ensure_binding_running", ready)
    monkeypatch.setattr("tmuxbot.dispatch.tmux_send_text", send_text)

    asyncio.run(
        dispatch_incoming_text(
            Frontend(),
            OmpBackend(),
            binding,
            SimpleNamespace(pending_rename={}),
            1,
            8024,
            "/plan finalize",
        )
    )

    assert not any(call[0] == "inject" for call in calls)
    assert len(calls) == 1
    assert "OMP Plan 模式未启用" in calls[0][3]
    assert "Alt+Shift+P" in calls[0][3]
    assert "自定义 keybindings" in calls[0][3]
    assert binding.tmux_target in calls[0][3]
    assert "已确认" not in calls[0][3]


def test_omp_tui_key_command_is_rejected_with_ssh_notice(monkeypatch):
    binding = _binding()
    binding.backend = "omp"
    calls = []

    class OmpBackend(_Backend):
        name = "omp"
        running_command_names = frozenset({"omp"})

    class Frontend:
        async def send_html(self, chat_id, thread_id, text):
            calls.append((chat_id, thread_id, text))

    async def ready(*_args, **_kwargs):
        return True

    sent_keys = []
    monkeypatch.setattr("tmuxbot.dispatch.ensure_binding_running", ready)
    monkeypatch.setattr(
        "tmuxbot.command_adapter.tmux_send_key",
        lambda target, key: sent_keys.append((target, key)),
    )

    asyncio.run(
        dispatch_incoming_text(
            Frontend(),
            OmpBackend(),
            binding,
            SimpleNamespace(pending_rename={}),
            1,
            8024,
            "/down",
        )
    )

    assert sent_keys == []
    assert len(calls) == 1
    assert "SSH" in calls[0][2]
    assert binding.tmux_target in calls[0][2]


def test_omp_plan_help_reports_active_native_mode_without_injection(monkeypatch):
    binding = _binding()
    binding.backend = "omp"
    calls = []

    class OmpBackend(_Backend):
        name = "omp"
        running_command_names = frozenset({"omp"})

        def read_plan_mode(self, _binding):
            return SimpleNamespace(status="active")

    class Frontend:
        async def send_html(self, chat_id, thread_id, text):
            calls.append(("reply", chat_id, thread_id, text))

    async def ready(*_args, **_kwargs):
        return True

    async def send_text(*_args, **_kwargs):
        calls.append(("inject",))

    monkeypatch.setattr("tmuxbot.dispatch.ensure_binding_running", ready)
    monkeypatch.setattr("tmuxbot.dispatch.tmux_send_text", send_text)

    asyncio.run(
        dispatch_incoming_text(
            Frontend(),
            OmpBackend(),
            binding,
            SimpleNamespace(pending_rename={}),
            1,
            8024,
            "/plan",
        )
    )

    assert not any(call[0] == "inject" for call in calls)
    assert len(calls) == 1
    assert "OMP Plan 模式已启用" in calls[0][3]
    assert "未向 OMP pane 注入" in calls[0][3]


def test_channel_fork_arms_session_handoff_before_tmux_injection(monkeypatch):
    binding = _binding()
    sent = []

    class Backend(_Backend):
        def command_opts(self):
            return {"/fork": CmdOpts(expect_session_handoff=True)}

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
            "/fork",
        )
    )

    assert binding.pending_session_handoff_after is not None
    assert sent[0][1] == "/fork"


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

    async def restart(*_args, **_kwargs):
        calls.append("restart")
        return False

    class Frontend:
        async def send_html(self, *_args):
            calls.append("reply")

    monkeypatch.setattr("tmuxbot.dispatch.restart_binding", restart)
    monkeypatch.setattr("tmuxbot.dispatch.tmux_send_key", lambda *_args: calls.append("key"))

    asyncio.run(
        dispatch_incoming_text(
            Frontend(),
            _Backend(),
            b,
            SimpleNamespace(pending_rename={}),
            1,
            None,
            "/restart",
        )
    )

    assert calls == ["restart", "reply"]


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


def test_omp_normal_message_enables_busy_submission(monkeypatch):
    b = _binding()
    b.backend = "omp"
    calls = []

    class OmpBackend(_Backend):
        name = "omp"
        running_command_names = frozenset({"omp"})

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
            OmpBackend(),
            b,
            SimpleNamespace(pending_rename={}),
            1,
            None,
            "在当前工作后继续检查",
        )
    )

    assert calls[0][1]["allow_busy_submission"] is True


def test_omp_pending_rename_enables_busy_submission(monkeypatch):
    b = _binding()
    b.backend = "omp"
    calls = []

    class OmpBackend(_Backend):
        name = "omp"
        running_command_names = frozenset({"omp"})

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
            OmpBackend(),
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
