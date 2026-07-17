from tmuxbot.providers.adapters import (
    get_provider_adapter,
    managed_provider_names,
    provider_capabilities,
)


def test_adapter_registry_keeps_launch_and_model_picker_details_server_side():
    claude = get_provider_adapter("claude")

    assert claude is not None
    assert claude.launch_arguments == ("--dangerously-skip-permissions",)
    assert claude.model_command == "/model"
    assert "Bash" in claude.teamrun_instruction
    assert managed_provider_names() == frozenset({"claude", "codex"})


def test_provider_capabilities_do_not_expose_launch_arguments():
    assert provider_capabilities("codex") == {
        "display_name": "Codex",
        "managed": True,
        "supports_model_picker": True,
        "model_command": "/model",
    }
    assert provider_capabilities("unknown")["managed"] is False
