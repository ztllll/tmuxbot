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
