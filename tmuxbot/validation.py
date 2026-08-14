"""Startup validation for tmuxbot configuration.

The daemon is intentionally permissive at the transport layer, but bindings are
the safety boundary. Validate them before starting frontends so a bad config does
not become a cross-chat or cross-project runtime problem.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

from tmuxbot.state import Binding

SUPPORTED_CHANNELS = frozenset({"telegram", "feishu"})
SUPPORTED_BACKENDS = frozenset({"claude_code", "codex", "omp"})


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


def validate_bindings(bindings: list[Binding], *, require_nonempty: bool = True) -> None:
    """Validate binding invariants.

    Raises:
        ConfigValidationError: if one or more binding errors are found.
    """
    errors: list[str] = []
    if require_nonempty and not bindings:
        errors.append("bindings.yaml must contain at least one binding")

    names: dict[str, Binding] = {}
    sources: dict[tuple[str, str, str, int | str | None], Binding] = {}
    tmux_targets: dict[tuple[str, int, int], Binding] = {}
    cwd_by_backend: dict[tuple[str, str], Binding] = {}
    admins: list[Binding] = []

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
        if b.admin:
            admins.append(b)
            if b.thread_id is not None:
                errors.append(f"binding {label!r}: admin route thread_id must be null")
            if b.channel == "telegram" and isinstance(b.chat_id, int) and b.chat_id <= 0:
                errors.append(
                    f"binding {label!r}: telegram admin route chat_id must be a positive private user id"
                )

        if b.channel == "telegram":
            if b.thread_root_message_id is not None:
                errors.append(
                    f"binding {label!r}: thread_root_message_id is only valid for Feishu topics"
                )
            if not isinstance(b.chat_id, int):
                errors.append(
                    f"binding {label!r}: telegram chat_id must be an integer, got {b.chat_id!r}"
                )
            if b.thread_id is not None and not isinstance(b.thread_id, int):
                errors.append(f"binding {label!r}: telegram thread_id must be an integer or null")

        if b.channel == "feishu":
            if not isinstance(b.chat_id, str) or not b.chat_id:
                errors.append(f"binding {label!r}: feishu chat_id must be a non-empty string")
            if b.thread_id is not None and not isinstance(b.thread_id, str):
                errors.append(f"binding {label!r}: feishu thread_id must be a string or null")
            if b.thread_root_message_id is not None and b.thread_id is None:
                errors.append(
                    f"binding {label!r}: Feishu thread_root_message_id requires thread_id"
                )
            if b.thread_root_message_id is not None and not b.thread_root_message_id.startswith(
                "om_"
            ):
                errors.append(
                    f"binding {label!r}: Feishu thread_root_message_id must start with 'om_'"
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

        if b.tmux_window < 0 or b.tmux_pane < 0:
            errors.append(f"binding {label!r}: tmux_window and tmux_pane must be >= 0")

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

    if len(admins) > 1:
        errors.append(
            f"only one admin route is allowed, found {[binding.name for binding in admins]}"
        )

    if errors:
        raise ConfigValidationError(errors)
