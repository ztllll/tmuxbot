import asyncio
from types import SimpleNamespace

from tmuxbot.picker import detect_idle_picker, extract_picker_block
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


def test_pi_plan_mode_command_routes_menu_and_direct_subcommands_correctly():
    backend = FakeBackend()
    backend.name = "pi"

    menu = classify_command(backend, "/plan", "")
    tools = classify_command(backend, "/plan", "tools")
    assert menu.kind == CommandKind.INTERACTIVE
    assert tools.kind == CommandKind.INTERACTIVE
    assert "pi-plan-mode" in menu.description
    for args in (
        "start",
        "show",
        "finalize",
        "implement",
        "save",
        "export PLAN.md",
        "exit",
        "off",
        "设计认证模块重构方案",
    ):
        assert classify_command(backend, "/plan", args).kind == CommandKind.PASSTHROUGH


def test_pi_builtin_command_matrix_uses_pi_specific_interactions():
    backend = FakeBackend()
    backend.name = "pi"

    assert classify_command(backend, "/model").kind == CommandKind.INTERACTIVE
    assert classify_command(backend, "/scoped-models").kind == CommandKind.INTERACTIVE
    assert classify_command(backend, "/settings").kind == CommandKind.INTERACTIVE
    assert classify_command(backend, "/resume").kind == CommandKind.INTERACTIVE
    assert classify_command(backend, "/tree").kind == CommandKind.INTERACTIVE
    assert classify_command(backend, "/fork").kind == CommandKind.INTERACTIVE
    assert classify_command(backend, "/trust").kind == CommandKind.INTERACTIVE
    assert classify_command(backend, "/todos").kind == CommandKind.PASSTHROUGH
    assert classify_command(backend, "/statusline").kind == CommandKind.INTERACTIVE
    assert classify_command(backend, "/name").kind == CommandKind.PASSTHROUGH
    assert classify_command(backend, "/session").kind == CommandKind.PASSTHROUGH
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

    binding = SimpleNamespace(name="pi-route", tmux_target="pi-route:0.0")
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


def test_picker_detector_uses_numbered_telegram_card_and_feishu_interaction_card(
    monkeypatch
):
    raw = (
        "Plan mode\n"
        "→ Start Plan mode\n"
        "  Choose tools, then start…\n"
        "↑/↓ navigate • enter select • esc close\n"
    )
    monkeypatch.setattr("tmuxbot.picker.tmux_capture", lambda *_args: raw)
    binding = SimpleNamespace(
        name="pi-route", chat_id="oc_alpha", thread_id="omt_plan", tmux_target="pi:0.0"
    )

    class State:
        picker_notified = {}

    telegram_calls = []

    class TelegramFrontend:
        name = "telegram"

        async def send_picker_card(self, *args, **kwargs):
            telegram_calls.append((args, kwargs))

    asyncio.run(detect_idle_picker(binding, State(), TelegramFrontend()))
    assert len(telegram_calls) == 1
    assert "下方 1-9 按钮" in telegram_calls[0][0][2]

    State.picker_notified = {}
    feishu_calls = []

    class FeishuFrontend:
        name = "feishu"

        async def send_interaction_card(self, *args, **kwargs):
            feishu_calls.append((args, kwargs))

    asyncio.run(detect_idle_picker(binding, State(), FeishuFrontend()))
    assert len(feishu_calls) == 1
    assert feishu_calls[0][0][0:2] == ("oc_alpha", "omt_plan")
    assert "使用下方方向键" in feishu_calls[0][0][2]
    assert feishu_calls[0][0][3] == "pi-route"


def test_pi_plan_mode_picker_footer_is_detected_for_remote_control():
    raw = (
        "Plan mode\n"
        "→ Start Plan mode\n"
        "  Choose tools, then start…\n"
        "↑/↓ navigate • enter select • esc close\n"
    )

    block = extract_picker_block(raw)

    assert block is not None
    assert "Start Plan mode" in block
    assert "enter select" in block


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
