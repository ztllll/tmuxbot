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

from tmuxbot.attachments import split_outbound_attachments
from tmuxbot.config import save_binding_identity
from tmuxbot.core.event_reducer import reduce_provider_event
from tmuxbot.core.replies import ReplyEnvelope
from tmuxbot.picker import detect_idle_picker
from tmuxbot.tmux import tmux_capture
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
                    # 初次启动 (last_file is None) → 跳末尾, 防 bootstrap 时把历史积压
                    # 一次性回吐撞 flood; 运行中切到新会话 (/clear /new, last_file 已有)
                    # → 从 0 读全, 否则新会话首条回复在 tailer 切过来前已落盘 → 被跳过 →
                    # Boss 收不到 /new 后第一条回复。新会话很小无 flood 风险,
                    # JSONL_BACKLOG_LIMIT 仍兜底意外大文件。
                    state.offsets[key] = 0 if last_file is not None else jl.stat().st_size
                    save_offsets(offsets_file, state.offsets, force=True)
                last_file = jl
                identity = backend.session_identity(b, jl)
                old_identity = (b.provider_session_id, b.transcript_path)
                b.provider_session_id = identity.session_id
                b.transcript_path = Path(identity.transcript_path) if identity.transcript_path else jl
                b.last_session_id = identity.session_id
                if old_identity != (b.provider_session_id, b.transcript_path):
                    await asyncio.to_thread(
                        save_binding_identity,
                        getattr(frontend, "bindings_file", None),
                        b,
                    )

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
                    events = backend.parse_event(
                        line, provider_session_id=b.provider_session_id
                    )
                    # ★ 同一 binding 内串行 await, 避免 aggregator race condition
                    # (旧 S.fire 并发让多个 event 同时拿到 agg=None, 各自新建 → 多条"工作中"卡片)
                    # 串行只影响本 binding tailer 实时性, 不影响其他 binding 并发
                    for event in events:
                        for reduced in reduce_provider_event(event):
                            try:
                                await on_tmux_event(
                                    b,
                                    reduced.kind,
                                    reduced.body,
                                    frontend,
                                    state,
                                    backend,
                                )
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
        await _send_html_with_outbound_attachments(frontend, b, body)
        return

    if kind == "assistant_plan":
        await _send_or_edit_plan(frontend, b, state, body)
        return

    if kind == "assistant_live_text":
        log.info(f"[{b.name}] assistant live text len={len(body)}")
        await _send_live_text(frontend, b, state, body, backend)
        return

    if kind == "assistant_text_delta":
        log.info(f"[{b.name}] assistant text delta len={len(body)}")
        await _append_reply_stream(frontend, b, state, body)
        return

    if kind == "assistant_text":
        log.info(f"[{b.name}] assistant final text len={len(body)}")
        # ★ 真说话 → 单独发新消息触发 TG 通知, 不动 aggregator
        # 剥掉 claude 手写 footer + 从 harness 任务文件渲染任务 footer 追加 (§6)
        text = strip_handwritten_footer(body)
        footer = render_task_footer(backend.read_tasks(b))
        out = f"{text}\n\n{footer}" if footer else text
        if out.strip():
            if await _finalize_reply_stream(frontend, b, state, out):
                return
            if _consume_recent_live_text(state, b, out):
                return
            await _send_assistant_reply(frontend, b, out, backend)
        return

    if kind != "assistant_tools":
        # 未知 kind, 兜底直发
        await _send_html_with_outbound_attachments(frontend, b, body)
        return

    # 走到这: kind == "assistant_tools", 进 aggregator
    body, attachments = split_outbound_attachments(body)
    if not body.strip():
        await _send_outbound_attachments(frontend, b, attachments)
        return

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
        if msg is not None and hasattr(msg, "message_id"):
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
    await _send_outbound_attachments(frontend, b, attachments)


async def _send_html_with_outbound_attachments(
    frontend: "Frontend", b: "Binding", html_text: str,
) -> None:
    clean_text, attachments = split_outbound_attachments(html_text)
    if clean_text.strip():
        await frontend.send_html(b.chat_id, b.thread_id, clean_text)
    await _send_outbound_attachments(frontend, b, attachments)


async def _send_assistant_reply(
    frontend: "Frontend", b: "Binding", html_text: str, backend: "Backend",
) -> None:
    clean_text, attachments = split_outbound_attachments(html_text)
    try:
        pane = await asyncio.to_thread(tmux_capture, b.tmux_target, 30)
        status = backend.parse_terminal_status(pane)
    except Exception:
        log.exception("[%s] provider status capture failed", b.name)
        status = None
    envelope = ReplyEnvelope(
        title="回复",
        body=clean_text,
        footer=status,
        attachments=tuple(str(a.path) for a in attachments),
        actions=("screen", "status", "cancel", "interrupt"),
    )
    await frontend.send_assistant_reply(b, envelope)


async def _send_live_text(
    frontend: "Frontend", b: "Binding", state: "State", html_text: str,
    backend: "Backend",
) -> None:
    await _send_assistant_reply(frontend, b, html_text, backend)
    _remember_live_text(state, b, html_text)


async def _append_reply_stream(
    frontend: "Frontend", b: "Binding", state: "State", delta_html: str,
) -> None:
    streams = getattr(state, "reply_streams", None)
    if streams is None:
        streams = {}
        setattr(state, "reply_streams", streams)

    current = streams.get(b.name)
    if current is None:
        msg = await frontend.send_html(b.chat_id, b.thread_id, delta_html)
        if msg is not None and hasattr(msg, "message_id"):
            streams[b.name] = {
                "msg_id": msg.message_id,
                "chat_id": b.chat_id,
                "content": delta_html,
            }
        return

    current["content"] = current.get("content", "") + delta_html
    await frontend.edit_html(current["chat_id"], current["msg_id"], current["content"])


async def _finalize_reply_stream(
    frontend: "Frontend", b: "Binding", state: "State", html_text: str,
) -> bool:
    streams = getattr(state, "reply_streams", None)
    if not streams:
        return False
    current = streams.pop(b.name, None)
    if not current:
        return False
    try:
        if current.get("content") != html_text:
            await frontend.edit_html(current["chat_id"], current["msg_id"], html_text)
        _remember_live_text(state, b, html_text)
        return True
    except Exception:
        log.exception(f"[{b.name}] finalize reply stream err; sending final text")
        return False


def _remember_live_text(state: "State", b: "Binding", html_text: str) -> None:
    recent = getattr(state, "live_text_recent", None)
    if recent is None:
        recent = {}
        setattr(state, "live_text_recent", recent)
    items = recent.setdefault(b.name, [])
    normalized = _normalize_live_text(html_text)
    if normalized and normalized not in items:
        items.append(normalized)
        del items[:-20]


def _consume_recent_live_text(state: "State", b: "Binding", html_text: str) -> bool:
    recent = getattr(state, "live_text_recent", None)
    if not recent:
        return False
    items = recent.get(b.name) or []
    normalized = _normalize_live_text(html_text)
    if normalized not in items:
        return False
    items.remove(normalized)
    return True


def _normalize_live_text(html_text: str) -> str:
    return "\n".join(line.rstrip() for line in html_text.strip().splitlines())


async def _send_or_edit_plan(
    frontend: "Frontend", b: "Binding", state: "State", html_text: str,
) -> None:
    plan_messages = getattr(state, "plan_messages", None)
    if plan_messages is None:
        plan_messages = {}
        setattr(state, "plan_messages", plan_messages)

    current = plan_messages.get(b.name)
    if current and current.get("content") == html_text:
        return

    if current and current.get("msg_id") is not None:
        try:
            await frontend.edit_html(current["chat_id"], current["msg_id"], html_text)
            current["content"] = html_text
            return
        except Exception:
            log.exception(f"[{b.name}] edit plan err; sending a new plan card")

    msg = await frontend.send_html(b.chat_id, b.thread_id, html_text)
    if msg is not None and hasattr(msg, "message_id"):
        plan_messages[b.name] = {
            "msg_id": msg.message_id,
            "chat_id": b.chat_id,
            "content": html_text,
        }


async def _send_outbound_attachments(
    frontend: "Frontend", b: "Binding", attachments,
) -> None:
    for attachment in attachments:
        if attachment.kind == "image":
            await frontend.send_image(b.chat_id, b.thread_id, attachment.path)
        else:
            await frontend.send_file(b.chat_id, b.thread_id, attachment.path)
