"""jsonl tailer + 工具调用聚合器 + assistant 事件路由。

★ 工具调用聚合 (Boss 需求):
- assistant_tools 事件 (thinking + tool_use) → 累计到一条可编辑消息
- assistant_text 事件 (真说话) → 封闭聚合器, 单独发新消息
- aggregator 累计字符 > 3500 或 30s 静默 → 自动封闭
"""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING

from tmuxbot.picker import detect_idle_picker
from tmuxbot.utils import render_task_footer, save_offsets, strip_handwritten_footer

if TYPE_CHECKING:
    from tmuxbot.backends.base import Backend
    from tmuxbot.frontends.base import Frontend
    from tmuxbot.state import Binding, State

log = logging.getLogger("tmuxbot")

JSONL_POLL = 0.5
AGGREGATOR_MAX_CHARS = 3500    # 累计超此长度封闭, 开新 aggregator
AGGREGATOR_IDLE_SECONDS = 15   # 静默超此秒数 = turn 结束, watcher 自动封闭
# ★ 积压保护阈值: 单次发现 jsonl 落盘新增超此字节数, 判定为「事务式 flush 爆发」
# (claude TUI 在派 subagent / 超长 turn 时不实时落盘, 完成后一次性 flush 数 MB)。
# 逐条推这种积压会瞬间撞 Telegram flood control → 直接跳末尾不回吐。正常单 turn 远 < 此值。
JSONL_BACKLOG_LIMIT = 512 * 1024   # 512KB


async def jsonl_poll_loop(
    b: "Binding", backend: "Backend", frontend: "Frontend",
    state: "State", offsets_file: Path,
) -> None:
    """0.5s 轮询 binding 当前 jsonl, 新事件 fire-and-forget 推给 on_tmux_event。"""
    log.info(f"[{b.name}] tailer start, backend={backend.name}")
    last_file: Path | None = None
    tick = 0
    last_hb = time.time()
    last_sz_logged: int = -1
    last_picker_check: float = 0.0
    last_sz_change: float = time.time()
    while True:
        # ★ binding 被 deprovision (群解散 / bot 被移除) 从 frontend.bindings 移除后,
        # tailer 下一轮自行退出 (不再 tail 已拆除会话的 jsonl)。
        if b not in frontend.bindings:
            log.info(f"[{b.name}] binding 已注销, tailer 退出")
            return
        tick += 1
        now = time.time()
        if now - last_hb > 60:
            log.info(f"[{b.name}] tailer alive · tick={tick} · bg_tasks={len(state.bg_tasks)}")
            last_hb = now
        try:
            jl = backend.find_active_jsonl(b)
            if jl is None:
                await asyncio.sleep(JSONL_POLL)
                continue
            key = str(jl)
            if jl != last_file:
                if last_file is not None:
                    log.info(f"[{b.name}] jsonl switch: {last_file.name} → {jl.name}")
                if key not in state.offsets:
                    state.offsets[key] = jl.stat().st_size
                    save_offsets(offsets_file, state.offsets, force=True)
                last_file = jl
                b.last_session_id = jl.stem

            sz = jl.stat().st_size
            if sz != last_sz_logged:
                log.info(f"[{b.name}] jsonl size {last_sz_logged} → {sz} (Δ{sz - last_sz_logged})")
                last_sz_logged = sz
                last_sz_change = now
            else:
                if now - last_sz_change > 5 and now - last_picker_check > 3:
                    last_picker_check = now
                    state.fire(detect_idle_picker(b, state, frontend))
            off = state.offsets.get(key, sz)
            if sz < off:
                off = 0
            # ★ 积压保护: 一次性落盘超 JSONL_BACKLOG_LIMIT (事务式 flush 爆发, 典型为
            # 自指会话里派 subagent 后整段 flush)。逐条推会撞 Telegram flood control →
            # 跳末尾, 发一条提示, 不回吐积压 (TUI 里看得到, 不需要 TG 重放)。
            if sz - off > JSONL_BACKLOG_LIMIT:
                skipped = sz - off
                log.warning(
                    f"[{b.name}] backlog {skipped}B > {JSONL_BACKLOG_LIMIT}B 一次性落盘, "
                    f"跳末尾防 flood (off {off} → {sz})"
                )
                state.offsets[key] = sz
                save_offsets(offsets_file, state.offsets, force=True)
                try:
                    await frontend.send_html(
                        b.chat_id, b.thread_id,
                        f"⚠️ 检测到 <b>{skipped // 1024}KB</b> 内容一次性落盘, "
                        f"已跳过未推送 (防 Telegram 限流)\n如需查看请到 TUI",
                    )
                except Exception:
                    log.debug(f"[{b.name}] backlog notice send err")
                await asyncio.sleep(JSONL_POLL)
                continue
            if sz > off:
                with open(jl, "rb") as f:
                    f.seek(off)
                    new_bytes = f.read()
                text = new_bytes.decode("utf-8", errors="replace")
                lines = text.split("\n")
                safe_off = off
                for i, line in enumerate(lines):
                    is_last = i == len(lines) - 1
                    if is_last:
                        break
                    safe_off += len(line.encode("utf-8")) + 1
                    if not line.strip():
                        continue
                    events = backend.parse_event(line)
                    # ★ 同一 binding 内串行 await, 避免 aggregator race condition
                    # (旧 S.fire 并发让多个 event 同时拿到 agg=None, 各自新建 → 多条"工作中"卡片)
                    # 串行只影响本 binding tailer 实时性, 不影响其他 binding 并发
                    for kind, body in events:
                        try:
                            await on_tmux_event(b, kind, body, frontend, state, backend)
                        except Exception:
                            log.exception(f"[{b.name}] on_tmux_event err")
                state.offsets[key] = safe_off
                save_offsets(offsets_file, state.offsets)
        except Exception:
            log.exception(f"[{b.name}] poll err")
        await asyncio.sleep(JSONL_POLL)


async def _close_aggregator(b: "Binding", state: "State", frontend: "Frontend") -> None:
    """把 aggregator 标记完成 (编辑消息加 ✓), 然后从 state 移除"""
    agg = state.tool_aggregator.pop(b.name, None)
    if not agg:
        return
    closing = "\n".join(agg["content"]) + "\n\n<i>✓ 完成</i>"
    try:
        await frontend.edit_html(agg["chat_id"], agg["msg_id"], closing)
    except Exception:
        log.exception(f"[{b.name}] close aggregator err")


async def _aggregator_idle_watcher(
    b: "Binding", state: "State", frontend: "Frontend",
) -> None:
    """背景 task: 等 AGGREGATOR_IDLE_SECONDS 秒后, 如果还是同一个 aggregator, 自动封闭。
    每次新 event 进来会刷 last_ts, watcher 重新计时。"""
    while True:
        await asyncio.sleep(AGGREGATOR_IDLE_SECONDS)
        agg = state.tool_aggregator.get(b.name)
        if agg is None:
            return  # 已被别处封闭
        if (time.time() - agg["last_ts"]) >= AGGREGATOR_IDLE_SECONDS:
            await _close_aggregator(b, state, frontend)
            return


async def on_tmux_event(
    b: "Binding", kind: str, body: str,
    frontend: "Frontend", state: "State", backend: "Backend",
) -> None:
    """JSONL tailer → TG 路由 (★ Boss 最终定型规则)。

    - user: 不回声 (Boss 自己注入的)
    - assistant_tools (thinking + tool_use): 进 aggregator 一条消息**流式 edit**
        不触发 TG push 通知 (Boss 静态看一条不断刷新的"工作中"卡片)
    - assistant_text (真说话): **单独发新消息**, 触发 TG push 通知 → Boss 收到提醒
        不关闭 aggregator (让工具调用继续累计到同一条)
    - aggregator 关闭时机:
        ① watcher 静默 AGGREGATOR_IDLE_SECONDS 秒后自动 close 加 ✓
        ② 累计 > AGGREGATOR_MAX_CHARS 字符主动 close 开新
    - attachment: 立刻单独发, 不动 aggregator (上下文外事件)
    """
    if state.setup_mode:
        return
    if kind == "user":
        return

    if not body.strip():
        return

    now = time.time()

    if kind == "attachment":
        await frontend.send_html(b.chat_id, b.thread_id, body)
        return

    if kind == "assistant_text":
        # ★ 真说话 → 单独发新消息触发 TG 通知, 不动 aggregator
        # 剥掉 claude 手写 footer + 从 harness 任务文件渲染任务 footer 追加 (§6)
        text = strip_handwritten_footer(body)
        footer = render_task_footer(backend.read_tasks(b))
        out = f"{text}\n\n{footer}" if footer else text
        if out.strip():
            await frontend.send_html(b.chat_id, b.thread_id, out)
        return

    if kind != "assistant_tools":
        # 未知 kind, 兜底直发
        await frontend.send_html(b.chat_id, b.thread_id, body)
        return

    # 走到这: kind == "assistant_tools", 进 aggregator
    agg = state.tool_aggregator.get(b.name)
    if agg and sum(len(s) for s in agg["content"]) > AGGREGATOR_MAX_CHARS:
        # 累计超长 → 主动 close 开新
        await _close_aggregator(b, state, frontend)
        agg = None

    if agg is None:
        # 新建 aggregator: 发首条 + 缓存 msg_id + 启动 idle watcher
        header = "💭 <b>工作中…</b>"
        initial_html = header + "\n" + body
        msg = await frontend.send_html(b.chat_id, b.thread_id, initial_html)
        if msg is None or not hasattr(msg, "message_id"):
            return
        state.tool_aggregator[b.name] = {
            "msg_id": msg.message_id,
            "chat_id": b.chat_id,
            "content": [header, body],
            "last_ts": now,
        }
        state.fire(_aggregator_idle_watcher(b, state, frontend))
    else:
        agg["content"].append(body)
        agg["last_ts"] = now
        new_html = "\n".join(agg["content"])
        await frontend.edit_html(agg["chat_id"], agg["msg_id"], new_html)
