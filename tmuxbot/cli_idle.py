"""Provider CLI-only idle hibernation.

This module deliberately separates provider lifetime from tmux lifetime:
- missing tmux targets stay dormant and are never recreated here;
- a shell-only pane is already warm and needs no action;
- only a positively identified, continuously idle provider TUI may be asked to
  exit, leaving its tmux pane and route intact.

IM timestamps are not an input.  The idle clock starts only after the live TUI
is observed in an explicit IDLE state and is reset by provider work or any
unsafe/ambiguous state.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Callable

from tmuxbot.command_adapter import detect_interaction_state
from tmuxbot.core.events import TerminalState
from tmuxbot.tmux import (
    _active_input_text,
    tmux_capture,
    tmux_has_session,
    tmux_pane_command,
)

if TYPE_CHECKING:
    from tmuxbot.backends.base import Backend
    from tmuxbot.control_plane.repository import ControlPlaneRepository
    from tmuxbot.frontends.base import Frontend
    from tmuxbot.state import Binding, State

log = logging.getLogger("tmuxbot")

DEFAULT_CLI_IDLE_TIMEOUT = 3600.0
DEFAULT_CLI_IDLE_INTERVAL = 30.0
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_WAITING_INTERACTIONS = frozenset({"plan_approval", "permission_prompt"})


class CliActivity(str, Enum):
    WORKING = "working"
    IDLE = "idle"
    DRAFT = "draft"
    INTERACTION = "interaction"
    WAITING = "waiting"
    SHELL = "shell"
    ABSENT = "absent"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class CliObservation:
    activity: CliActivity
    detail: str = ""


def cli_idle_enabled() -> bool:
    raw = os.getenv("TMUXBOT_CLI_IDLE_ENABLED", "1").strip().lower()
    return raw in _TRUE_VALUES


def cli_idle_timeout() -> float:
    return _positive_env_seconds(
        "TMUXBOT_CLI_IDLE_TIMEOUT", DEFAULT_CLI_IDLE_TIMEOUT, minimum=0.0
    )


def cli_idle_interval() -> float:
    return _positive_env_seconds(
        "TMUXBOT_CLI_IDLE_INTERVAL", DEFAULT_CLI_IDLE_INTERVAL, minimum=5.0
    )


def _positive_env_seconds(name: str, default: float, *, minimum: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return max(minimum, float(raw))
    except ValueError:
        log.warning("invalid %s=%r, using %.1fs", name, raw, default)
        return default


def observe_cli(binding: "Binding", backend: "Backend") -> CliObservation:
    """Classify one live pane without creating or modifying anything."""
    if not tmux_has_session(binding.tmux_session):
        return CliObservation(CliActivity.ABSENT, "tmux session missing")

    command = tmux_pane_command(binding.tmux_target)
    if command in backend.shell_command_names:
        return CliObservation(CliActivity.SHELL, command)
    if not backend.is_running_command(command):
        return CliObservation(CliActivity.UNKNOWN, command or "no foreground command")

    pane = tmux_capture(binding.tmux_target, 100)
    draft = _active_input_text(pane)
    if draft and draft.strip():
        return CliObservation(CliActivity.DRAFT, draft[:160])

    interaction = detect_interaction_state(pane)
    if interaction.kind in _WAITING_INTERACTIONS:
        return CliObservation(CliActivity.WAITING, interaction.kind)
    if interaction.kind != "generic":
        return CliObservation(CliActivity.INTERACTION, interaction.kind)

    status = backend.parse_terminal_status(pane)
    if status is None:
        return CliObservation(CliActivity.UNKNOWN, "terminal status unavailable")
    if status.state == TerminalState.WORKING:
        return CliObservation(CliActivity.WORKING, status.label)
    if status.state in {TerminalState.WAITING, TerminalState.BLOCKED}:
        return CliObservation(CliActivity.WAITING, status.label or status.state.value)
    if status.state == TerminalState.IDLE:
        return CliObservation(CliActivity.IDLE, status.label)
    return CliObservation(CliActivity.UNKNOWN, status.state.value)


async def reconcile_cli_idle_once(
    frontends: list["Frontend"],
    state: "State",
    repository: "ControlPlaneRepository",
    *,
    timeout: float | None = None,
    now: float | None = None,
    observer: Callable[["Binding", "Backend"], CliObservation] = observe_cli,
) -> None:
    """Run one non-invasive idle pass over all exact routes."""
    idle_timeout = cli_idle_timeout() if timeout is None else max(0.0, timeout)
    current = time.monotonic() if now is None else now
    protected_targets = set(repository.active_teamrun_tmux_targets())

    seen: set[str] = set()
    for frontend in list(frontends):
        for binding in list(getattr(frontend, "bindings", ())):
            if binding.name in seen:
                continue
            seen.add(binding.name)
            backend = frontend.backend_for(binding)
            binding_timeout = (
                idle_timeout
                if binding.cli_idle_timeout_seconds is None
                else float(binding.cli_idle_timeout_seconds)
            )
            await _reconcile_binding(
                binding,
                backend,
                state,
                protected_targets,
                timeout=binding_timeout,
                now=current,
                observer=observer,
            )


def _runtime_blocked(binding: "Binding", state: "State", protected: set[str]) -> bool:
    return bool(
        binding.tmux_target in protected
        or binding.name in state.pending_rename
        or binding.name in state.command_transactions
        or binding.pending_session_handoff_after is not None
    )


async def _reconcile_binding(
    binding: "Binding",
    backend: "Backend",
    state: "State",
    protected_targets: set[str],
    *,
    timeout: float,
    now: float,
    observer: Callable[["Binding", "Backend"], CliObservation],
) -> None:
    if timeout == 0 or _runtime_blocked(binding, state, protected_targets):
        state.cli_idle_since.pop(binding.name, None)
        return

    lock = state.ensure_locks.setdefault(binding.name, asyncio.Lock())
    if lock.locked():
        # Incoming delivery/restart owns this same route lock.  Do not wait and
        # risk hibernating immediately after a newly delivered message.
        state.cli_idle_since.pop(binding.name, None)
        return

    async with lock:
        if _runtime_blocked(binding, state, protected_targets):
            state.cli_idle_since.pop(binding.name, None)
            return
        observation = observer(binding, backend)
        if observation.activity != CliActivity.IDLE:
            state.cli_idle_since.pop(binding.name, None)
            return

        since = state.cli_idle_since.get(binding.name)
        if since is None:
            state.cli_idle_since[binding.name] = now
            return
        if now - since < timeout:
            return

        # Re-observe under the route lifecycle lock immediately before exit.
        final = observer(binding, backend)
        if final.activity != CliActivity.IDLE:
            state.cli_idle_since.pop(binding.name, None)
            return
        result = await backend.hibernate(binding)
        if result:
            state.cli_idle_since.pop(binding.name, None)
            state.tui_fp.pop(binding.name, None)
            log.info(
                "[%s] provider CLI hibernated after %.0fs continuous TUI idle",
                binding.name,
                now - since,
            )
        else:
            # Do not hammer a provider whose native exit could not be verified.
            state.cli_idle_since[binding.name] = now
            log.warning("[%s] provider CLI hibernation was not verified", binding.name)


async def cli_idle_loop(
    frontends: list["Frontend"],
    state: "State",
    repository: "ControlPlaneRepository",
    *,
    interval: float | None = None,
    startup_delay: float = 5.0,
) -> None:
    if not cli_idle_enabled():
        log.info("CLI idle hibernation disabled by TMUXBOT_CLI_IDLE_ENABLED")
        return
    every = cli_idle_interval() if interval is None else max(5.0, interval)
    log.info(
        "CLI idle hibernation starting · timeout=%.0fs · interval=%.1fs",
        cli_idle_timeout(),
        every,
    )
    if startup_delay > 0:
        await asyncio.sleep(startup_delay)
    while True:
        try:
            await reconcile_cli_idle_once(frontends, state, repository)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("CLI idle reconciliation failed")
        await asyncio.sleep(every)
