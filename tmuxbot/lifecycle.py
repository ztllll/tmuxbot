"""tmux/CLI 生命周期巡检。

现有消息入口会按需调用 backend.ensure_running()。本模块把同一能力提升为后台
watchdog: 按 bindings 周期性确认 tmux session、pane 内 CLI 都处于可用状态。
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

DEFAULT_LIFECYCLE_INTERVAL = 30.0
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


async def ensure_binding_running(
    backend: "Backend",
    b: "Binding",
    state: "State",
    *,
    reason: str,
    wait: bool = True,
) -> bool:
    """串行调用 backend.ensure_running。

    Args:
        wait: False 时如果已有同 binding ensure 在跑, 直接跳过。后台巡检用 False,
            用户消息入口用 True, 保证消息注入前 CLI 已 ready。

    Returns:
        True 表示实际执行了 ensure_running; False 表示被跳过。
    """
    lock = state.ensure_locks.setdefault(b.name, asyncio.Lock())
    if not wait and lock.locked():
        log.debug("[%s] lifecycle ensure skipped: already running", b.name)
        return False

    async with lock:
        started = time.monotonic()
        await backend.ensure_running(b)
        elapsed = time.monotonic() - started
        if elapsed >= 1.0:
            log.info(
                "[%s] ensure_running(%s) finished in %.1fs", b.name, reason, elapsed
            )
        return True


async def lifecycle_watch_loop(
    frontends: list["Frontend"],
    state: "State",
    *,
    interval: float | None = None,
    startup_delay: float = DEFAULT_STARTUP_DELAY,
) -> None:
    """按 frontend/binding 周期性恢复 tmux session 和 CLI。

    frontend 持有 backend 与 bindings 子集, 所以这里以 frontend 为巡检单位,
    避免从全局 binding 再反查 backend。
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
        for fe in list(frontends):
            backend = getattr(fe, "backend", None)
            if backend is None:
                continue
            for b in list(getattr(fe, "bindings", [])):
                checked += 1
                try:
                    await ensure_binding_running(
                        backend, b, state, reason="watchdog", wait=False
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("[%s] lifecycle ensure failed", b.name)
        log.debug("lifecycle watchdog tick · checked=%d", checked)
        await asyncio.sleep(every)
