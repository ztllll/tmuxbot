from tmuxbot.providers.adapters import (
    get_provider_adapter,
    managed_provider_names,
    provider_launch_arguments,
    provider_capabilities,
)


def test_adapter_registry_keeps_launch_and_model_picker_details_server_side():
    claude = get_provider_adapter("claude")

    assert claude is not None
    assert claude.launch_arguments == ("--dangerously-skip-permissions",)
    assert claude.model_command == "/model"
    assert "Bash" in claude.teamrun_instruction
    assert managed_provider_names() == frozenset({"claude", "codex", "pi"})


def test_provider_capabilities_do_not_expose_launch_arguments():
    assert provider_capabilities("codex") == {
        "display_name": "Codex",
        "managed": True,
        "supports_model_picker": True,
        "model_command": "/model",
    }
    assert provider_capabilities("unknown")["managed"] is False


def test_provider_launch_arguments_uses_the_registered_dynamic_resolver(monkeypatch):
    monkeypatch.setattr(
        "tmuxbot.providers.adapters.codex_launch_arguments",
        lambda: ("--dangerously-bypass-approvals-and-sandbox", "-m", "configured-model"),
    )

    assert provider_launch_arguments("codex") == (
        "--dangerously-bypass-approvals-and-sandbox",
        "-m",
        "configured-model",
    )
    assert provider_launch_arguments("claude") == ("--dangerously-skip-permissions",)
    assert provider_launch_arguments("pi") == ("--approve",)
    assert provider_launch_arguments("unknown") is None
