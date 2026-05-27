"""活性指示心跳: 仅当 TUI 状态行的「时间/tokens」还在动时发 sendChatAction(typing)。

判定: 每 4s tick, 调 backend.find_tui_activity_fp(pane) 抓状态行指纹;
     指纹变了/刚出现 → 更新 last_active;
     ACTIVE_WINDOW (10s) 内活跃过 → 发 typing。
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from tmuxbot.tmux import tmux_capture

if TYPE_CHECKING:
    from tmuxbot.backends.base import Backend
    from tmuxbot.frontends.base import Frontend
    from tmuxbot.state import State

log = logging.getLogger("tmuxbot")

HEARTBEAT_INTERVAL = 4   # typing TG 端显示 ~5s, 每 4s 刷新
ACTIVE_WINDOW = 10       # 距上次活跃小于这个秒数才发 typing


async def heartbeat_typing_loop(state: "State", frontend) -> None:
    """活性指示主循环。每个 frontend 一个 loop, 用 frontend.backend + frontend.bindings。
    bot 死 → typing 5s 内消失。"""
    backend = frontend.backend
    while True:
        try:
            await asyncio.sleep(HEARTBEAT_INTERVAL)
            if state.setup_mode:
                continue
            now = time.time()
            for b in frontend.bindings:
                if b.chat_id == 0:
                    continue
                try:
                    pane = tmux_capture(b.tmux_target, lines=15)
                except Exception as e:
                    log.debug(f"[{b.name}] heartbeat capture err: {e}")
                    pane = ""
                fp = backend.find_tui_activity_fp(pane)
                last_fp = state.tui_fp.get(b.name)
                if fp:
                    if fp != last_fp:
                        state.tui_fp[b.name] = fp
                        state.last_active[b.name] = now
                else:
                    state.tui_fp.pop(b.name, None)
                ts = state.last_active.get(b.name, 0)
                if now - ts > ACTIVE_WINDOW:
                    continue
                await frontend.send_chat_action(b.chat_id, b.thread_id, "typing")
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("heartbeat loop err")
            await asyncio.sleep(HEARTBEAT_INTERVAL)
