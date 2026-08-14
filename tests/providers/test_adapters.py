import pytest

from tmuxbot.providers.adapters import (
    get_provider_adapter,
    managed_provider_names,
    provider_launch_arguments,
    provider_capabilities,
)


def test_adapter_registry_keeps_launch_route_and_credential_details_server_side():
    claude = get_provider_adapter("claude")
    codex = get_provider_adapter("codex")
    omp = get_provider_adapter("omp")

    assert claude is not None
    assert claude.route_backend == "claude_code"
    assert claude.telegram_credential_env == "TG_BOT_TOKEN"
    assert claude.launch_arguments == ("--dangerously-skip-permissions",)
    assert claude.model_command == "/model"
    assert "Bash" in claude.teamrun_instruction
    assert codex is not None
    assert codex.route_backend == "codex"
    assert codex.telegram_credential_env == "TG_CODEX_BOT_TOKEN"
    assert omp is not None
    assert omp.display_name == "Oh My Pi"
    assert omp.route_backend == "omp"
    assert omp.telegram_credential_env == "TG_OMP_BOT_TOKEN"
    assert "bash" in omp.teamrun_instruction
    assert managed_provider_names() == frozenset({"claude", "codex", "omp"})


def test_provider_capabilities_do_not_expose_launch_arguments():
    assert provider_capabilities("codex") == {
        "display_name": "Codex",
        "managed": True,
        "supports_model_picker": True,
        "model_command": "/model",
    }
    assert provider_capabilities("unknown")["managed"] is False
    assert provider_capabilities("omp")["display_name"] == "Oh My Pi"
    assert provider_capabilities("pi") == {
        "display_name": "pi",
        "managed": False,
        "supports_model_picker": False,
        "model_command": None,
    }


def test_provider_launch_arguments_uses_registered_dynamic_resolvers(tmp_path, monkeypatch):
    extension = tmp_path / "tmuxbot-session-handoff.ts"
    extension.write_text("export default () => {};\n", encoding="utf-8")
    monkeypatch.setattr(
        "tmuxbot.providers.adapters.codex_launch_arguments",
        lambda: ("--dangerously-bypass-approvals-and-sandbox", "-m", "configured-model"),
    )
    monkeypatch.setattr("tmuxbot.providers.adapters.managed_extension_source", lambda: extension)

    assert provider_launch_arguments("codex") == (
        "--dangerously-bypass-approvals-and-sandbox",
        "-m",
        "configured-model",
    )
    assert provider_launch_arguments("claude") == ("--dangerously-skip-permissions",)
    omp_arguments = provider_launch_arguments("omp")
    assert omp_arguments == (
        "--approval-mode",
        "yolo",
        "--extension",
        str(extension.resolve()),
    )
    assert not {"--approve", "--session", "--continue", "--mode", "-p"}.intersection(omp_arguments)
    assert provider_launch_arguments("unknown") is None


def test_omp_launch_arguments_fail_closed_when_extension_is_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "tmuxbot.providers.adapters.managed_extension_source",
        lambda: tmp_path / "missing.ts",
    )

    with pytest.raises(OSError, match="managed OMP handoff extension missing"):
        provider_launch_arguments("omp")
