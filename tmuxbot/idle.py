"""Idle-kill watcher: 闲置超阈值自动发双 Ctrl-C 优雅杀掉 claude TUI。

来消息时 ensure_running 会自动 `--resume` 重生, 上下文不丢。

**默认行为**: `idle_kill_seconds=0` → 永不触发。
必须在 bindings.yaml 里显式配 `idle_kill_seconds: <秒数>` 才会 opt-in。
这是自指开发会话的安全保护 — 不 opt-in 就绝对不会被杀。
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from tmuxbot.tmux import tmux_capture, tmux_pane_command, tmux_send_key

if TYPE_CHECKING:
    from tmuxbot.frontends.base import Frontend
    from tmuxbot.state import State

log = logging.getLogger("tmuxbot")

IDLE_CHECK_INTERVAL = 60  # 秒, 每次 tick 间隔


async def idle_kill_loop(state: "State", frontend: "Frontend") -> None:
    """Idle-kill 主循环。每个 frontend 一个 loop, 用 frontend.backend + frontend.bindings。

    每 IDLE_CHECK_INTERVAL 秒检查一次:
    - 跳过未 opt-in (idle_kill_seconds <= 0) 的 binding
    - 跳过正在生成 (find_tui_activity_fp 非 None) 的 pane
    - 跳过 pane 已经不是 claude 的情况 (已被杀或从未启动)
    - 发双 Ctrl-C → 2.5s 后复查 → log 结果
    """
    backend = frontend.backend
    started_at = time.time()  # 没有活跃记录时用此时间作为基准, 防刚启动就判 idle

    while True:
        try:
            await asyncio.sleep(IDLE_CHECK_INTERVAL)
            if state.setup_mode:
                continue
            now = time.time()
            for b in frontend.bindings:
                if b.idle_kill_seconds <= 0:
                    continue
                if b.chat_id == 0:
                    continue

                # 计算 idle 时长: 没有活跃记录的用 started_at 作基准
                last_ts = state.last_active.get(b.name, started_at)
                idle = now - last_ts
                if idle < b.idle_kill_seconds:
                    continue

                # 抓屏判 busy: 正在生成中不杀
                try:
                    pane = tmux_capture(b.tmux_target, lines=15)
                except Exception as e:
                    log.debug(f"[{b.name}] idle-kill capture err: {e}")
                    continue
                if backend.find_tui_activity_fp(pane) is not None:
                    continue

                # 判 pane 是否在跑 claude: 已经不是 claude 则无需再杀
                current_cmd = tmux_pane_command(b.tmux_target)
                if current_cmd != backend.pane_command_name:
                    continue

                # 执行优雅杀: 双 Ctrl-C
                log.info(
                    f"[{b.name}] idle {idle:.0f}s >= {b.idle_kill_seconds}s, "
                    f"发双 Ctrl-C 优雅杀 claude..."
                )
                tmux_send_key(b.tmux_target, "C-c")
                await asyncio.sleep(0.4)
                tmux_send_key(b.tmux_target, "C-c")

                # 2.5s 后复查结果
                await asyncio.sleep(2.5)
                try:
                    after_cmd = tmux_pane_command(b.tmux_target)
                except Exception as e:
                    log.debug(f"[{b.name}] idle-kill post-check err: {e}")
                    continue

                if after_cmd != backend.pane_command_name:
                    log.info(
                        f"[{b.name}] idle {idle:.0f}s > {b.idle_kill_seconds}s, "
                        f"claude 已优雅杀 (来消息会自动 --resume 重生)"
                    )
                else:
                    log.warning(
                        f"[{b.name}] idle-kill 发了双 Ctrl-C 但 claude 仍在, "
                        f"跳过本轮 (下次 tick 重试)"
                    )

        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("idle_kill_loop err")
            await asyncio.sleep(IDLE_CHECK_INTERVAL)
