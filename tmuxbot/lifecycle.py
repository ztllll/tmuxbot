"""tmux/CLI 生命周期巡检。

消息入口会按需调用 backend.ensure_running()。后台健康巡检每小时检查已存在
的 route pane；人工关闭的 tmux 不会被它重建，只在下一条消息到达时按需恢复。
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tmuxbot.backends.base import Backend
    from tmuxbot.frontends.base import Frontend
    from tmuxbot.state import Binding, State

log = logging.getLogger("tmuxbot")

DEFAULT_LIFECYCLE_INTERVAL = 3600.0
DEFAULT_STARTUP_DELAY = 3.0


def lifecycle_enabled() -> bool:
    raw = os.getenv("TMUXBOT_LIFECYCLE_ENABLED", "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def lifecycle_interval() -> float:
    raw = os.getenv("TMUXBOT_LIFECYCLE_INTERVAL", "")
    if not raw:
        return DEFAULT_LIFECYCLE_INTERVAL
    try:
        return max(5.0, float(raw))
    except ValueError:
        log.warning(
            "invalid TMUXBOT_LIFECYCLE_INTERVAL=%r, using %.1fs",
            raw,
            DEFAULT_LIFECYCLE_INTERVAL,
        )
        return DEFAULT_LIFECYCLE_INTERVAL


async def _ensure_backend_running(backend: "Backend", b: "Binding", *, reason: str) -> None:
    try:
        await backend.ensure_running(b)
    except Exception as first_error:
        recover = getattr(backend, "recover_unhealthy_pane", None)
        if not callable(recover):
            raise
        try:
            recovered = await recover(b)
        except Exception:
            log.exception("[%s] unhealthy-pane recovery failed", b.name)
            raise first_error
        if not recovered:
            raise
        log.warning("[%s] recovered unsafe provider pane for %s", b.name, reason)


async def ensure_binding_running(
    backend: "Backend",
    b: "Binding",
    state: "State",
    *,
    reason: str,
    wait: bool = True,
) -> bool:
    """Serialize provider startup and health recovery for one binding."""
    lock = state.ensure_locks.setdefault(b.name, asyncio.Lock())
    if not wait and lock.locked():
        log.debug("[%s] lifecycle ensure skipped: already running", b.name)
        return False

    async with lock:
        started = time.monotonic()
        await _ensure_backend_running(backend, b, reason=reason)
        elapsed = time.monotonic() - started
        if elapsed >= 1.0:
            log.info("[%s] ensure_running(%s) finished in %.1fs", b.name, reason, elapsed)
        return True


async def restart_binding(
    backend: "Backend",
    b: "Binding",
    state: "State",
    *,
    reason: str,
    fresh: bool = False,
    delay: float = 0.0,
) -> bool:
    """Restart one route under its lifecycle lock.

    ``fresh`` deliberately drops the provider identity before the clean respawn;
    it is only used by OMP's `/exit` configuration/extension reload path. The
    optional delay is held under the same route lock so later IM cannot reach the
    provider which is about to exit.

    Returns True when an existing provider was restarted and False when a missing
    provider/session was only started. OMP opts into a clean pane respawn so IM
    restart never injects Ctrl-C/Ctrl-D into its native TUI.
    """
    from tmuxbot.tmux import (
        tmux_has_session,
        tmux_pane_command,
        tmux_respawn_pane,
        tmux_send_key,
    )

    lock = state.ensure_locks.setdefault(b.name, asyncio.Lock())
    async with lock:
        if delay:
            await asyncio.sleep(delay)
        if fresh:
            b.provider_session_id = None
            b.last_session_id = None
            b.transcript_path = None
            b.pending_session_handoff_after = None
            b.fresh_start_pending = True
        if not tmux_has_session(b.tmux_session):
            await _ensure_backend_running(backend, b, reason=reason)
            return False

        command = tmux_pane_command(b.tmux_target)
        was_running = backend.is_running_command(command)
        if backend.restart_via_clean_respawn:
            if not tmux_respawn_pane(b.tmux_target, b.cwd):
                raise RuntimeError(f"failed to respawn provider pane {b.tmux_target}")
            await asyncio.sleep(0.25)
        elif was_running:
            tmux_send_key(b.tmux_target, "C-c")
            await asyncio.sleep(0.5)
            tmux_send_key(b.tmux_target, "C-d")
            await asyncio.sleep(2.0)

        await _ensure_backend_running(backend, b, reason=reason)
        return was_running


async def lifecycle_watch_loop(
    frontends: list["Frontend"],
    state: "State",
    *,
    interval: float | None = None,
    startup_delay: float = DEFAULT_STARTUP_DELAY,
) -> None:
    """低频巡检已存在的 route pane，并在 provider 异常时受控恢复。

    巡检绝不新建缺失的 tmux session，仍尊重 `/tmuxstop` 和人工关闭；已存在
    pane 内的 shell、崩溃或不安全 provider 进程树会交给 adapter 重新验证/恢复。
    """
    if not lifecycle_enabled():
        log.info("lifecycle watchdog disabled by TMUXBOT_LIFECYCLE_ENABLED")
        return

    every = interval if interval is not None else lifecycle_interval()
    log.info("lifecycle watchdog starting · interval=%.1fs", every)
    if startup_delay > 0:
        await asyncio.sleep(startup_delay)

    while True:
        checked = 0
        from tmuxbot.tmux import tmux_has_session

        for fe in list(frontends):
            for b in list(getattr(fe, "bindings", [])):
                if not tmux_has_session(b.tmux_session):
                    log.debug("[%s] lifecycle health skipped: tmux session absent", b.name)
                    continue
                checked += 1
                try:
                    backend = fe.backend_for(b)
                    await ensure_binding_running(
                        backend, b, state, reason="health-audit", wait=False
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("[%s] lifecycle ensure failed", b.name)
        log.debug("lifecycle watchdog tick · checked=%d", checked)
        await asyncio.sleep(every)
