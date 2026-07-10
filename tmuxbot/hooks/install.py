"""Idempotently merge tmuxbot-owned Claude hooks into settings.json."""

from __future__ import annotations

import json
import os
import shlex
import sys
import tempfile
from copy import deepcopy
from pathlib import Path

from tmuxbot.hooks.claude import SUPPORTED_HOOK_EVENTS


OWNED_COMMAND_FRAGMENT = "tmuxbot.hooks.claude"


def _owned_hook_command() -> str:
    script = Path(__file__).with_name("claude.py")
    return (
        f"env TMUXBOT_HOOK_OWNER={OWNED_COMMAND_FRAGMENT} "
        f"{shlex.quote(sys.executable)} {shlex.quote(str(script))}"
    )


def _is_owned_hook(hook: object) -> bool:
    return isinstance(hook, dict) and OWNED_COMMAND_FRAGMENT in str(hook.get("command", ""))


def merge_claude_hooks(settings: dict) -> dict:
    merged = deepcopy(settings)
    hooks = merged.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        hooks = {}
        merged["hooks"] = hooks
    owned = {"type": "command", "command": _owned_hook_command()}
    for event_name in sorted(SUPPORTED_HOOK_EVENTS):
        existing = hooks.get(event_name, [])
        if not isinstance(existing, list):
            existing = []
        preserved = []
        for matcher in existing:
            if not isinstance(matcher, dict):
                preserved.append(matcher)
                continue
            matcher_copy = deepcopy(matcher)
            nested = matcher_copy.get("hooks", [])
            removed_owned = False
            if isinstance(nested, list):
                filtered = [hook for hook in nested if not _is_owned_hook(hook)]
                removed_owned = len(filtered) != len(nested)
                matcher_copy["hooks"] = filtered
            if (
                removed_owned
                and not matcher_copy.get("hooks")
                and set(matcher_copy).issubset({"matcher", "hooks"})
            ):
                continue
            if matcher_copy.get("hooks") or any(k != "hooks" for k in matcher_copy):
                preserved.append(matcher_copy)
        preserved.append({"matcher": "", "hooks": [owned]})
        hooks[event_name] = preserved
    return merged


def install_claude_hooks(
    *, settings_path: Path | None = None, dry_run: bool = False
) -> dict:
    path = settings_path or (Path.home() / ".claude" / "settings.json")
    if path.is_file():
        try:
            current = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            current = {}
    else:
        current = {}
    if not isinstance(current, dict):
        current = {}
    merged = merge_claude_hooks(current)
    if dry_run:
        return merged
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        json.dump(merged, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        temp_path = Path(handle.name)
    os.replace(temp_path, path)
    return merged
