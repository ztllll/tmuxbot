import asyncio
from pathlib import Path
from types import SimpleNamespace

from tmuxbot.backends.omp import OmpBackend
from tmuxbot.picker import detect_idle_picker, detect_omp_interaction
from tmuxbot.command_adapter import (
    CommandKind,
    action_from_command,
    binding_by_token,
    binding_token,
    classify_command,
    detect_interaction_state,
    parse_slash_text,
    semantic_action_from_command,
    semantic_actions_from_body,
    probe_passthrough_result,
)


class FakeBackend:
    name = "claude_code"
    remote_tui_actions_allowed = True

    def interactive_commands(self):
        if self.name == "codex":
            return {"/stop": "stop terminal"}
        return {"/model": "model picker"}

    def command_opts(self):
        return {"/context": object(), "/clear": object()}

    def command_aliases(self):
        return {"/new": "/clear"}


def test_parse_slash_strips_bot_suffix_and_applies_alias():
    parsed = parse_slash_text(
        "/new@my_bot keep old name",
        bot_username="my_bot",
        aliases=FakeBackend().command_aliases(),
    )

    assert parsed is not None
    assert parsed.command == "/new"
    assert parsed.raw_command == "/new@my_bot"
    assert parsed.injected_text == "/clear keep old name"
    assert parsed.args == "keep old name"


def test_classify_known_capture_interactive_blocked_and_unknown():
    backend = FakeBackend()

    assert classify_command(backend, "/context").kind == CommandKind.CAPTURE
    assert classify_command(backend, "/model").kind == CommandKind.INTERACTIVE
    assert classify_command(backend, "/tmuxstop").kind == CommandKind.LOCAL
    codex = FakeBackend()
    codex.name = "codex"
    assert classify_command(codex, "/stop").kind == CommandKind.INTERACTIVE
    assert classify_command(backend, "/logout").kind == CommandKind.BLOCKED
    assert classify_command(backend, "/whatever").kind == CommandKind.PASSTHROUGH


def test_omp_plan_command_is_always_tmuxbot_local_help():
    backend = OmpBackend()

    for args in (
        "",
        "tools",
        "start",
        "show",
        "finalize",
        "implement",
        "save",
        "export PLAN.md",
        "exit",
        "设计认证模块重构方案",
    ):
        spec = classify_command(backend, "/plan", args)
        assert spec.kind == CommandKind.LOCAL
        assert "不向 pane 注入" in spec.description


def test_omp_builtin_command_matrix_uses_native_interactions_and_capture_commands():
    backend = OmpBackend()

    assert classify_command(backend, "/model").kind == CommandKind.INTERACTIVE
    assert classify_command(backend, "/scoped-models").kind == CommandKind.INTERACTIVE
    assert classify_command(backend, "/settings").kind == CommandKind.INTERACTIVE
    assert classify_command(backend, "/resume").kind == CommandKind.INTERACTIVE
    assert classify_command(backend, "/tree").kind == CommandKind.INTERACTIVE
    assert classify_command(backend, "/trust").kind == CommandKind.INTERACTIVE
    assert classify_command(backend, "/statusline").kind == CommandKind.INTERACTIVE
    for command in ("/new", "/fork", "/compact", "/clear", "/fresh"):
        assert classify_command(backend, command).kind == CommandKind.CAPTURE
    assert classify_command(backend, "/quit").kind == CommandKind.BLOCKED


def test_tui_action_commands():
    assert action_from_command("/down", "") == "down"
    assert action_from_command("/key", "return") == "enter"
    assert action_from_command("/key", "escape") == "esc"
    assert action_from_command("/key", "space") == "space"
    assert semantic_action_from_command("/approve-plan") == "approve-plan"
    assert classify_command(FakeBackend(), "/approve-plan").kind == CommandKind.LOCAL


def test_passthrough_probe_always_reports_a_tui_receipt(monkeypatch):
    sent = []

    class Frontend:
        async def send_html(self, chat_id, thread_id, text):
            sent.append((chat_id, thread_id, text))

    binding = SimpleNamespace(name="omp-route", tmux_target="omp-route:0.0")
    monkeypatch.setattr(
        "tmuxbot.command_adapter.tmux_capture",
        lambda *_args: "Reloaded extensions\n›",
    )

    asyncio.run(
        probe_passthrough_result(
            Frontend(),
            binding,
            123,
            None,
            "/reload",
            "before",
            delay=0,
        )
    )

    assert len(sent) == 1
    assert "/reload 已执行，TUI 回执如下" in sent[0][2]
    assert "Reloaded extensions" in sent[0][2]


def test_binding_token_round_trip():
    bindings = [SimpleNamespace(name="alpha"), SimpleNamespace(name="beta")]
    token = binding_token("beta")

    assert binding_by_token(bindings, token).name == "beta"
    assert binding_by_token(bindings, "missing") is None


def test_omp_picker_detector_only_sends_static_ssh_notice(monkeypatch):
    raw = (Path(__file__).parent / "fixtures" / "omp" / "resume_picker.txt").read_text()
    monkeypatch.setattr("tmuxbot.picker.tmux_capture", lambda *_args: raw)
    binding = SimpleNamespace(
        name="omp-route",
        chat_id="oc_alpha",
        thread_id="omt_plan",
        tmux_target="omp:0.0",
        tmux_session="omp",
        backend="omp",
    )

    class State:
        picker_notified = {}

    calls = []

    class Frontend:
        name = "feishu"

        def backend_for(self, _binding):
            return OmpBackend()

        async def send_html(self, *args, **kwargs):
            calls.append((args, kwargs))

        async def send_picker_card(self, *_args, **_kwargs):
            raise AssertionError("OMP picker must not send a numbered card")

        async def send_interaction_card(self, *_args, **_kwargs):
            raise AssertionError("OMP picker must not send a remote-control card")

    asyncio.run(detect_idle_picker(binding, State(), Frontend()))

    assert len(calls) == 1
    assert calls[0][0][0:2] == ("oc_alpha", "omt_plan")
    assert "需要交互式操作" in calls[0][0][2]
    assert "SSH" in calls[0][0][2]
    assert "tmux select-window" in calls[0][0][2]
    assert "omp:0.0" in calls[0][0][2]


def test_omp_resume_picker_requires_native_footer_and_classifies_selection():
    menu = "Resume session\n→ Native adapter migration\n↑/↓ navigate • enter select • esc close\n"
    assert detect_omp_interaction(menu) is None

    interaction = detect_omp_interaction(
        (Path(__file__).parent / "fixtures" / "omp" / "resume_picker.txt").read_text()
    )

    assert interaction is not None
    assert interaction.kind == "selection"
    assert interaction.label == "选择菜单"
    assert "Native adapter migration" in interaction.block


def test_omp_interaction_detector_classifies_input_and_rejects_historical_menu():
    footer = (
        "╭── π OMP 17.3.2 • session: native-adapter ╮\n╰─ ~/project (main) • high • 10.0%/360k ─╯\n"
    )
    input_screen = "Export plan\n> PLAN.md\nenter submit • esc back\n" + footer
    historical = (
        "Selection menu\n→ Implement here\n↑/↓ navigate • enter select • esc close\n"
        "assistant discussed the menu here\n" + footer
    )
    stale_footer = (
        "Selection menu\n→ Implement here\n↑/↓ navigate • enter select • esc close\n"
        + footer
        + "old output\n"
    )

    interaction = detect_omp_interaction(input_screen)

    assert interaction is not None
    assert interaction.kind == "text_input"
    assert interaction.label == "文本输入"
    assert detect_omp_interaction(historical) is None
    assert detect_omp_interaction(stale_footer) is None


def test_detects_plan_approval_state():
    state = detect_interaction_state(
        "Plan ready\nApprove and start coding\nKeep planning with feedback\nEsc to cancel"
    )

    assert state.kind == "plan_approval"
    assert [a.action for a in state.actions] == [
        "approve-plan",
        "revise-plan",
        "reject-plan",
    ]


def test_detects_permission_prompt_state():
    state = detect_interaction_state("Permission required\nApprove once\nDeny")

    assert state.kind == "permission_prompt"
    assert [a.action for a in state.actions] == ["approve-once", "deny"]


def test_permissions_menu_is_detected_as_picker():
    state = detect_interaction_state(
        "Permissions\nAuto\nRead Only\nEnter to select\n↑/↓ to navigate\nEsc to cancel"
    )

    assert state.kind == "picker"
    assert [a.action for a in state.actions] == ["select-current", "cancel"]


def test_semantic_actions_from_interaction_body():
    actions = semantic_actions_from_body(
        "语义操作: <code>/approve-plan</code> 批准计划 / <code>/reject-plan</code> 退出计划"
    )

    assert [a.action for a in actions] == ["approve-plan", "reject-plan"]
