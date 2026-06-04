from types import SimpleNamespace

from tmuxbot.command_adapter import (
    CommandKind,
    action_from_command,
    binding_by_token,
    binding_token,
    classify_command,
    parse_slash_text,
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


def test_binding_token_round_trip():
    bindings = [SimpleNamespace(name="alpha"), SimpleNamespace(name="beta")]
    token = binding_token("beta")

    assert binding_by_token(bindings, token).name == "beta"
    assert binding_by_token(bindings, "missing") is None
