"""Provider-specific CLI behaviour behind one small registry interface.

The control plane never accepts a browser-supplied launch command.  It asks this
registry how an already discovered provider should be started and which native
model command may be offered in the terminal workspace instead.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ProviderAdapter:
    binary_name: str
    display_name: str
    launch_arguments: tuple[str, ...]
    model_command: str | None
    teamrun_instruction: str

    @property
    def supports_model_picker(self) -> bool:
        return self.model_command is not None


_ADAPTERS = {
    "claude": ProviderAdapter(
        binary_name="claude",
        display_name="Claude Code",
        launch_arguments=("--dangerously-skip-permissions",),
        model_command="/model",
        teamrun_instruction="使用 Claude Code 的 Bash 工具执行 worker 回报命令。",
    ),
    "codex": ProviderAdapter(
        binary_name="codex",
        display_name="Codex",
        launch_arguments=("--dangerously-bypass-approvals-and-sandbox",),
        model_command="/model",
        teamrun_instruction="使用 Codex 的 shell 工具执行 worker 回报命令。",
    ),
}


def get_provider_adapter(binary_name: str) -> ProviderAdapter | None:
    """Return the approved adapter for a discovered executable, if any."""

    return _ADAPTERS.get(binary_name)


def managed_provider_names() -> frozenset[str]:
    """Names that can be launched as managed LLM sessions."""

    return frozenset(_ADAPTERS)


def provider_capabilities(binary_name: str) -> dict[str, object]:
    """Serialize only operator-facing capability metadata for the WebUI."""

    adapter = get_provider_adapter(binary_name)
    if adapter is None:
        return {
            "display_name": binary_name,
            "managed": False,
            "supports_model_picker": False,
            "model_command": None,
        }
    return {
        "display_name": adapter.display_name,
        "managed": True,
        "supports_model_picker": adapter.supports_model_picker,
        "model_command": adapter.model_command,
    }
