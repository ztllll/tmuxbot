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
from tmuxbot.utils import render_task_footer

if TYPE_CHECKING:
    from tmuxbot.state import State

log = logging.getLogger("tmuxbot")

HEARTBEAT_INTERVAL = 4  # typing TG 端显示 ~5s, 每 4s 刷新
ACTIVE_WINDOW = 10  # 距上次活跃小于这个秒数才发 typing


async def heartbeat_typing_loop(state: "State", frontend) -> None:
    """活性指示主循环。每个 route 用自己的 provider adapter。
    bot 死 → typing 5s 内消失。"""
    while True:
        try:
            await asyncio.sleep(HEARTBEAT_INTERVAL)
            if state.setup_mode:
                continue
            now = time.time()
            for b in frontend.bindings:
                if b.chat_id == 0:
                    continue
                backend = frontend.backend_for(b)
                try:
                    pane = tmux_capture(b.tmux_target, lines=15)
                except Exception as e:
                    log.debug(f"[{b.name}] heartbeat capture err: {e}")
                    pane = ""
                if backend.name == "omp":
                    await _sync_omp_compaction_status(state, frontend, b, backend, pane, now)
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


async def _sync_omp_compaction_status(state, frontend, b, backend, pane: str, now: float) -> None:
    status = backend.parse_terminal_status(pane)
    label = status.label.lower() if status is not None else ""
    compacting = "compacting" in label
    current = state.compaction_status.get(b.name)
    if compacting and current is None:
        estimate = backend.estimated_compaction_seconds(b)
        body = _compaction_body(b, backend, elapsed=0, estimate=estimate)
        state.compaction_status[b.name] = {
            "msg_id": None,
            "chat_id": b.chat_id,
            "started_at": now,
            "eta_seconds": estimate,
            "last_edit": now,
        }
        try:
            message = await frontend.send_status_html(
                b.chat_id,
                b.thread_id,
                body,
                display_state="working",
                footer=status,
            )
        except Exception:
            state.compaction_status.pop(b.name, None)
            raise
        state.compaction_status[b.name]["msg_id"] = getattr(message, "message_id", None)
        return
    if not compacting:
        if current is not None:
            absent_since = current.setdefault("absent_since", now)
            if now - absent_since < 8:
                return
            state.compaction_status.pop(b.name, None)
            body = (
                "⚠️ <b>OMP 自动压缩状态已结束，但未观察到压缩完成记录</b>\n"
                "· 任务可能没有自动续跑；请发送 <code>继续</code> 或用 <code>/screen</code> 检查 TUI"
            )
            footer = _omp_footer(b, backend)
            if footer:
                body += f"\n\n{footer}"
            if current.get("msg_id") is not None:
                finalize = getattr(frontend, "finalize_status_html", None)
                if callable(finalize):
                    await finalize(
                        current["chat_id"],
                        current["msg_id"],
                        body,
                        display_state="error",
                    )
                else:
                    await frontend.edit_html(current["chat_id"], current["msg_id"], body)
            else:
                await frontend.send_status_html(
                    b.chat_id,
                    b.thread_id,
                    body,
                    display_state="error",
                    footer=status,
                )
        return
    if current is None:
        return
    current.pop("absent_since", None)
    if now - current.get("last_edit", 0) < 12:
        return
    elapsed = max(0, round(now - current["started_at"]))
    body = _compaction_body(b, backend, elapsed=elapsed, estimate=current["eta_seconds"])
    if current.get("msg_id") is not None:
        await frontend.edit_html(current["chat_id"], current["msg_id"], body)
    current["last_edit"] = now


def _compaction_body(b, backend, *, elapsed: int, estimate: int) -> str:
    remaining = max(0, estimate - elapsed)
    if remaining:
        timing = f"已进行 <code>{elapsed}s</code> · 预计剩余约 <code>{remaining}s</code>"
    else:
        timing = f"已进行 <code>{elapsed}s</code> · 已超过历史估算，仍在等待 provider"
    footer = _omp_footer(b, backend)
    body = f"🗜 <b>OMP 正在自动压缩上下文</b>\n· {timing}\n· 新消息仍可发送，会排队到压缩结束后处理"
    return f"{body}\n\n{footer}" if footer else body


def _omp_footer(b, backend) -> str:
    render_extension = getattr(backend, "render_extension_footer", None)
    extension_footer = render_extension(b) if callable(render_extension) else ""
    task_footer = render_task_footer(backend.read_tasks(b), style="omp")
    return "\n\n".join(part for part in (extension_footer, task_footer) if part)
