"""Read Codex's own model default for every tmuxbot launch.

tmuxbot owns only the transport and permission flag.  The selected model remains
Codex configuration, so changing ``~/.codex/config.toml`` applies to both new
and resumed tmux sessions without a tmuxbot code/config change.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any


def codex_config_path() -> Path:
    return Path.home() / ".codex" / "config.toml"


def codex_model_from_config(path: Path | None = None) -> str | None:
    """Return top-level Codex ``model`` if the local TOML is readable."""
    config_path = path or codex_config_path()
    try:
        try:
            import tomllib
        except ImportError:  # pragma: no cover - Python 3.10 only
            import tomli as tomllib  # type: ignore[no-redef]
        with config_path.open("rb") as config_file:
            parsed: dict[str, Any] = tomllib.load(config_file)
    except (OSError, ValueError):
        return None
    model = parsed.get("model")
    return model.strip() if isinstance(model, str) and model.strip() else None


def codex_launch_arguments(path: Path | None = None) -> tuple[str, ...]:
    """Permission plus an explicit configured model when one is present."""
    arguments = ["--dangerously-bypass-approvals-and-sandbox"]
    model = codex_model_from_config(path)
    if model is not None:
        arguments.extend(("-m", model))
    return tuple(arguments)
