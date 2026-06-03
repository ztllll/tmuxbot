"""Startup validation for tmuxbot configuration.

The daemon is intentionally permissive at the transport layer, but bindings are
the safety boundary. Validate them before starting frontends so a bad config does
not become a cross-chat or cross-project runtime problem.
"""
from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Iterable

from tmuxbot.state import Binding

SUPPORTED_CHANNELS = frozenset({"telegram", "feishu"})
SUPPORTED_BACKENDS = frozenset({"claude_code", "codex"})
TELEGRAM_TOKEN_BACKENDS = {
    "TG_BOT_TOKEN": "claude_code",
    "TG_CODEX_BOT_TOKEN": "codex",
}


class ConfigValidationError(ValueError):
    """Raised when bindings contain unsafe or unsupported configuration."""

    def __init__(self, errors: Iterable[str]) -> None:
        self.errors = list(errors)
        super().__init__("\n".join(self.errors))


def _norm_path(path: Path) -> str:
    try:
        return str(path.expanduser().resolve())
    except OSError:
        return str(path.expanduser().absolute())


def validate_bindings(bindings: list[Binding]) -> None:
    """Validate binding invariants.

    Raises:
        ConfigValidationError: if one or more binding errors are found.
    """
    errors: list[str] = []
    if not bindings:
        errors.append("bindings.yaml must contain at least one binding")

    names: dict[str, Binding] = {}
    sources: dict[tuple[str, str, str, int | None], Binding] = {}
    sessions: dict[str, Binding] = {}
    tmux_targets: dict[tuple[str, int, int], Binding] = {}
    cwd_by_backend: dict[tuple[str, str], Binding] = {}
    feishu_backend_by_env: dict[str, str] = {}

    for idx, b in enumerate(bindings, start=1):
        label = b.name or f"#{idx}"

        if not b.name:
            errors.append(f"binding #{idx}: name is required")
        elif b.name in names:
            errors.append(
                f"binding {label!r}: duplicate name, first used by {names[b.name].name!r}"
            )
        else:
            names[b.name] = b

        if b.channel not in SUPPORTED_CHANNELS:
            errors.append(
                f"binding {label!r}: unsupported channel {b.channel!r}; "
                f"expected one of {sorted(SUPPORTED_CHANNELS)}"
            )

        if b.backend not in SUPPORTED_BACKENDS:
            errors.append(
                f"binding {label!r}: unsupported backend {b.backend!r}; "
                f"expected one of {sorted(SUPPORTED_BACKENDS)}"
            )

        if not b.bot_token_env:
            errors.append(f"binding {label!r}: bot_token_env is required")

        if b.channel == "telegram":
            if not isinstance(b.chat_id, int):
                errors.append(
                    f"binding {label!r}: telegram chat_id must be an integer, "
                    f"got {b.chat_id!r}"
                )
            expected = TELEGRAM_TOKEN_BACKENDS.get(b.bot_token_env)
            if expected is None:
                errors.append(
                    f"binding {label!r}: unknown telegram bot_token_env "
                    f"{b.bot_token_env!r}; add it to TOKEN_TO_BACKEND before use"
                )
            elif b.backend != expected:
                errors.append(
                    f"binding {label!r}: backend {b.backend!r} does not match "
                    f"{b.bot_token_env!r} backend {expected!r}"
                )

        if b.channel == "feishu":
            if not isinstance(b.chat_id, str) or not b.chat_id:
                errors.append(
                    f"binding {label!r}: feishu chat_id must be a non-empty string"
                )
            if b.thread_id is not None:
                errors.append(f"binding {label!r}: feishu thread_id must be null")
            prior = feishu_backend_by_env.get(b.bot_token_env)
            if prior is None:
                feishu_backend_by_env[b.bot_token_env] = b.backend
            elif prior != b.backend:
                errors.append(
                    f"binding {label!r}: feishu env {b.bot_token_env!r} mixes "
                    f"backend {prior!r} and {b.backend!r}"
                )

        source_key = (b.channel, b.bot_token_env, str(b.chat_id), b.thread_id)
        prior_source = sources.get(source_key)
        if prior_source is not None:
            errors.append(
                f"binding {label!r}: duplicate source "
                f"(channel={b.channel}, bot_token_env={b.bot_token_env}, "
                f"chat_id={b.chat_id}, thread_id={b.thread_id}) already used by "
                f"{prior_source.name!r}"
            )
        else:
            sources[source_key] = b

        if not b.tmux_session:
            errors.append(f"binding {label!r}: tmux_session is required")
        else:
            prior_session = sessions.get(b.tmux_session)
            if prior_session is not None:
                errors.append(
                    f"binding {label!r}: duplicate tmux_session "
                    f"{b.tmux_session!r} already used by {prior_session.name!r}"
                )
            else:
                sessions[b.tmux_session] = b

        if b.tmux_window < 0 or b.tmux_pane < 0:
            errors.append(
                f"binding {label!r}: tmux_window and tmux_pane must be >= 0"
            )

        target_key = (b.tmux_session, b.tmux_window, b.tmux_pane)
        prior_target = tmux_targets.get(target_key)
        if prior_target is not None:
            errors.append(
                f"binding {label!r}: duplicate tmux target {b.tmux_target!r} "
                f"already used by {prior_target.name!r}"
            )
        else:
            tmux_targets[target_key] = b

        if not str(b.cwd):
            errors.append(f"binding {label!r}: cwd is required")
        else:
            cwd_key = (b.backend, _norm_path(b.cwd))
            prior_cwd = cwd_by_backend.get(cwd_key)
            if prior_cwd is not None:
                errors.append(
                    f"binding {label!r}: duplicate cwd for backend {b.backend!r}: "
                    f"{cwd_key[1]!r} already used by {prior_cwd.name!r}"
                )
            else:
                cwd_by_backend[cwd_key] = b

    by_token: dict[str, set[str]] = defaultdict(set)
    for b in bindings:
        if b.channel == "telegram":
            by_token[b.bot_token_env].add(b.backend)
    for token_env, backend_names in by_token.items():
        if len(backend_names) > 1:
            errors.append(
                f"telegram bot_token_env {token_env!r} maps to multiple backends: "
                f"{sorted(backend_names)}"
            )

    if errors:
        raise ConfigValidationError(errors)
