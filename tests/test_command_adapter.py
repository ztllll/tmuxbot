from types import SimpleNamespace

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
    assert classify_command(backend, "/logout").kind == CommandKind.BLOCKED
    assert classify_command(backend, "/whatever").kind == CommandKind.PASSTHROUGH


def test_tui_action_commands():
    assert action_from_command("/down", "") == "down"
    assert action_from_command("/key", "return") == "enter"
    assert action_from_command("/key", "escape") == "esc"
    assert action_from_command("/key", "space") == "space"
    assert semantic_action_from_command("/approve-plan") == "approve-plan"
    assert classify_command(FakeBackend(), "/approve-plan").kind == CommandKind.LOCAL


def test_binding_token_round_trip():
    bindings = [SimpleNamespace(name="alpha"), SimpleNamespace(name="beta")]
    token = binding_token("beta")

    assert binding_by_token(bindings, token).name == "beta"
    assert binding_by_token(bindings, "missing") is None


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
