"""JSONL tailing and result-first provider-event delivery."""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from tmuxbot.attachments import split_outbound_attachments
from tmuxbot.config import save_binding_identity
from tmuxbot.core.im_delivery_audit import increment as audit_increment
from tmuxbot.core.im_presentation import IMPresentationPolicy
from tmuxbot.core.replies import ReplyEnvelope
from tmuxbot.core.events import ProviderRuntimeMetadata, TerminalState, TerminalStatus
from tmuxbot.core.runtime_v2 import RuntimeV2Router
from tmuxbot.core.turn_projection import ProgressIntent, TurnProjection
from tmuxbot.picker import detect_idle_picker
from tmuxbot.tmux import tmux_capture
from tmuxbot.utils import render_task_footer, save_offsets, strip_handwritten_footer

if TYPE_CHECKING:
    from tmuxbot.backends.base import Backend
    from tmuxbot.frontends.base import Frontend
    from tmuxbot.state import Binding, State

log = logging.getLogger("tmuxbot")

JSONL_POLL = 0.5
AGGREGATOR_IDLE_SECONDS = 15   # Legacy in-flight aggregator migration timeout.
# ★ 积压保护阈值: 单次发现 jsonl 落盘新增超此字节数, 判定为「事务式 flush 爆发」
# (claude TUI 在派 subagent / 超长 turn 时不实时落盘, 完成后一次性 flush 数 MB)。
# 逐条推这种积压会瞬间撞 Telegram flood control → 直接跳末尾不回吐。正常单 turn 远 < 此值。
JSONL_BACKLOG_LIMIT = 512 * 1024   # 512KB


def _initial_jsonl_offset(
    b: "Binding", transcript: Path, *, last_file: Path | None
) -> int:
    """Choose the first committed offset for one newly observed transcript.

    A bridge restart with a pinned provider identity must skip historical output.
    A running session switch and a freshly provisioned, still-unpinned route must
    start at zero: Pi can create and complete its first turn before the 0.5s
    tailer observes the new file, so treating that file as bootstrap history
    silently drops the project's first assistant reply.
    """
    if last_file is not None:
        return 0
    pinned_path = Path(b.transcript_path) if b.transcript_path else None
    if b.provider_session_id or pinned_path is not None:
        return transcript.stat().st_size
    return 0


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
            old_identity = (b.provider_session_id, b.transcript_path)
            provider_events = backend.poll_provider_events(b)
            if old_identity != (b.provider_session_id, b.transcript_path):
                await asyncio.to_thread(
                    save_binding_identity,
                    getattr(frontend, "bindings_file", None),
                    b,
                )
            await _dispatch_provider_events(
                b, provider_events, frontend, state, backend
            )
            jl = backend.find_active_jsonl(b)
            if jl is None:
                await asyncio.sleep(JSONL_POLL)
                continue
            key = str(jl)
            if jl != last_file:
                if last_file is not None:
                    log.info(f"[{b.name}] jsonl switch: {last_file.name} → {jl.name}")
                if key not in state.offsets:
                    # 已持久化 identity 的 bridge bootstrap 跳末尾，防历史回吐；运行中
                    # session switch 和刚 provision、尚无 identity 的 route 从 0 读取。
                    # 后者的首条 Pi turn 可能在 tailer 首次看见文件前已经完整落盘。
                    state.offsets[key] = _initial_jsonl_offset(
                        b, jl, last_file=last_file
                    )
                    save_offsets(offsets_file, state.offsets, force=True)
                last_file = jl
                identity = backend.session_identity(b, jl)
                old_identity = (b.provider_session_id, b.transcript_path)
                previous_session_id = b.provider_session_id
                b.provider_session_id = identity.session_id
                b.transcript_path = Path(identity.transcript_path) if identity.transcript_path else jl
                b.last_session_id = identity.session_id
                if old_identity != (b.provider_session_id, b.transcript_path):
                    await asyncio.to_thread(
                        save_binding_identity,
                        getattr(frontend, "bindings_file", None),
                        b,
                    )
                if (
                    b.pending_session_handoff_after is not None
                    and identity.session_id != previous_session_id
                ):
                    b.pending_session_handoff_after = None

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
                    delivered = await _dispatch_provider_events(
                        b, events, frontend, state, backend
                    )
                    if not delivered:
                        safe_off -= len(line.encode("utf-8")) + 1
                        break
                state.offsets[key] = safe_off
                save_offsets(offsets_file, state.offsets)
        except Exception:
            log.exception(f"[{b.name}] poll err")
        await asyncio.sleep(JSONL_POLL)


async def _dispatch_provider_events(
    b: "Binding", events, frontend: "Frontend", state: "State", backend: "Backend"
) -> bool:
    for event in events:
        if (
            event.kind.value == "provider_error"
            and getattr(backend, "name", None) == "pi"
            and callable(getattr(backend, "provider_error_is_managed", None))
            and backend.provider_error_is_managed(b)
        ):
            log.debug("[%s] Pi provider error deferred to terminal-health audit", b.name)
            continue
        decision = RuntimeV2Router.from_environment().route(event)
        for reduced in decision.deliveries:
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
                log.exception(f"[{b.name}] on_tmux_event err; retaining transcript offset")
                return False
    return True


def _audit(state: "State", b: "Binding", counter: str, amount: int = 1) -> None:
    registry = getattr(state, "im_delivery_metrics", None)
    if registry is None:
        registry = {}
        setattr(state, "im_delivery_metrics", registry)
    audit_increment(registry, b.name, counter, amount)


def _presentation_policy(state: "State") -> IMPresentationPolicy:
    policy = getattr(state, "im_presentation", None)
    if policy is None:
        policy = IMPresentationPolicy.from_environment()
        setattr(state, "im_presentation", policy)
    return policy


def _turn_projection(state: "State", b: "Binding") -> TurnProjection:
    projections = getattr(state, "turn_projections", None)
    if projections is None:
        projections = {}
        setattr(state, "turn_projections", projections)
    projection = projections.get(b.name)
    if projection is None:
        policy = _presentation_policy(state)
        projection = TurnProjection(
            progress_delay_seconds=policy.progress_delay_seconds,
            update_interval_seconds=policy.progress_update_interval_seconds,
            max_steps=policy.progress_max_steps,
        )
        projections[b.name] = projection
    return projection


async def _deliver_progress_intent(
    frontend: "Frontend", b: "Binding", state: "State", backend: "Backend",
    intent: ProgressIntent,
) -> None:
    messages = getattr(state, "progress_messages", None)
    if messages is None:
        messages = {}
        setattr(state, "progress_messages", messages)
    current = messages.get(b.name)
    if current is None:
        if intent.action == "finalize":
            return
        status = await _capture_terminal_status(b, backend)
        send_status = getattr(frontend, "send_status_html", None)
        if callable(send_status):
            msg = await send_status(
                b.chat_id,
                b.thread_id,
                intent.body_html,
                display_state=intent.display_state,
                footer=status,
            )
        else:
            msg = await frontend.send_html(b.chat_id, b.thread_id, intent.body_html)
        if msg is not None and hasattr(msg, "message_id"):
            messages[b.name] = {"msg_id": msg.message_id, "chat_id": b.chat_id}
            _audit(state, b, "progress_created")
        return
    if intent.action == "finalize":
        try:
            finalize = getattr(frontend, "finalize_status_html", None)
            if callable(finalize):
                await finalize(
                    current["chat_id"],
                    current["msg_id"],
                    intent.body_html,
                    display_state=intent.display_state,
                )
            else:
                await frontend.edit_html(
                    current["chat_id"], current["msg_id"], intent.body_html
                )
        except Exception:
            log.warning(
                "[%s] progress finalize failed; publishing replacement summary",
                b.name,
                exc_info=True,
            )
            await frontend.send_html(b.chat_id, b.thread_id, intent.body_html)
            _audit(state, b, "progress_recreated")
        finally:
            messages.pop(b.name, None)
            _audit(state, b, "progress_finalized")
        return
    try:
        await frontend.edit_html(current["chat_id"], current["msg_id"], intent.body_html)
        _audit(state, b, "progress_edited")
    except Exception:
        log.warning(
            "[%s] progress edit failed; recreating progress card", b.name, exc_info=True
        )
        messages.pop(b.name, None)
        msg = await frontend.send_html(b.chat_id, b.thread_id, intent.body_html)
        if msg is not None and hasattr(msg, "message_id"):
            messages[b.name] = {"msg_id": msg.message_id, "chat_id": b.chat_id}
            _audit(state, b, "progress_recreated")


async def _project_progress(
    frontend: "Frontend", b: "Binding", state: "State", backend: "Backend",
    kind: str, body: str, *, now: float,
) -> None:
    if not _presentation_policy(state).progress_enabled:
        return
    projection = _turn_projection(state, b)
    intents = projection.consume(kind, body, now=now)
    for intent in intents:
        await _deliver_progress_intent(frontend, b, state, backend, intent)
    delay = projection.next_update_in(now=now)
    if delay is not None and delay > 0:
        flushes = getattr(state, "progress_flushes", None)
        if flushes is None:
            flushes = {}
            setattr(state, "progress_flushes", flushes)
        task = flushes.get(b.name)
        if task is None or task.done():
            fire = getattr(state, "fire", None)
            coro = _flush_progress_after(frontend, b, state, backend, delay)
            if callable(fire):
                flushes[b.name] = fire(coro)
            else:
                coro.close()


async def _flush_progress_after(
    frontend: "Frontend", b: "Binding", state: "State", backend: "Backend",
    delay: float,
) -> None:
    try:
        await asyncio.sleep(delay)
        projection = getattr(state, "turn_projections", {}).get(b.name)
        if projection is None:
            return
        for intent in projection.flush(now=time.time()):
            await _deliver_progress_intent(frontend, b, state, backend, intent)
    finally:
        getattr(state, "progress_flushes", {}).pop(b.name, None)


async def _finalize_progress(
    frontend: "Frontend", b: "Binding", state: "State", backend: "Backend",
    *, display_state: str = "completed", heading: str | None = None,
) -> None:
    projection = getattr(state, "turn_projections", {}).pop(b.name, None)
    flush_task = getattr(state, "progress_flushes", {}).pop(b.name, None)
    if flush_task is not None and hasattr(flush_task, "cancel"):
        flush_task.cancel()
    if projection is None:
        return
    intents = (
        projection.finalize(now=time.time())
        if heading is None
        else projection.close_without_result(
            now=time.time(), display_state=display_state, heading=heading
        )
    )
    for intent in intents:
        await _deliver_progress_intent(frontend, b, state, backend, intent)


async def _close_aggregator(
    b: "Binding", state: "State", frontend: "Frontend", backend: "Backend | None" = None,
) -> None:
    """把 aggregator 标记完成 (编辑消息加 ✓), 然后从 state 移除"""
    aggregators = getattr(state, "tool_aggregator", None)
    if not aggregators:
        return
    agg = aggregators.pop(b.name, None)
    if not agg:
        return
    content = "\n".join(agg["content"]) + "\n\n<i>✓ 完成</i>"
    footer = _task_footer(b, backend) if backend is not None else agg.get("task_footer", "")
    closing = _append_footer(content, footer)
    try:
        finalize = getattr(frontend, "finalize_status_html", None)
        if callable(finalize):
            await finalize(agg["chat_id"], agg["msg_id"], closing)
        else:
            await frontend.edit_html(agg["chat_id"], agg["msg_id"], closing)
    except Exception:
        log.exception(f"[{b.name}] close aggregator err")


async def _aggregator_idle_watcher(
    b: "Binding", state: "State", frontend: "Frontend", backend: "Backend",
) -> None:
    """背景 task: 等 AGGREGATOR_IDLE_SECONDS 秒后, 如果还是同一个 aggregator, 自动封闭。
    每次新 event 进来会刷 last_ts, watcher 重新计时。"""
    while True:
        await asyncio.sleep(AGGREGATOR_IDLE_SECONDS)
        agg = state.tool_aggregator.get(b.name)
        if agg is None:
            return  # 已被别处封闭
        if (time.time() - agg["last_ts"]) >= AGGREGATOR_IDLE_SECONDS:
            await _close_aggregator(b, state, frontend, backend)
            return


def _delivery_binding(b: "Binding", state: "State") -> "Binding":
    """Project one topic-originated Admin turn back to its exact Feishu topic."""
    context = getattr(state, "admin_delivery_contexts", {}).get(b.name)
    if context is None:
        return b
    return replace(
        b,
        chat_id=context["chat_id"],
        thread_id=context["thread_id"],
        thread_root_message_id=context["thread_root_message_id"],
    )


async def on_tmux_event(
    b: "Binding", kind: str, body: str,
    frontend: "Frontend", state: "State", backend: "Backend",
) -> None:
    """Project normalized provider events into a result-first IM lifecycle.

    - user: never echoed;
    - tool/plan/lifecycle progress: one bounded editable card per route Turn;
    - final assistant text: finalize that progress card, then publish one result;
    - attachments: native channel delivery without expanding process chatter;
    - legacy aggregator state: finalized only for safe in-place upgrades.
    """
    if state.setup_mode:
        return
    b = _delivery_binding(b, state)
    if kind == "user":
        return

    if not body.strip():
        return

    now = time.time()

    if kind == "provider_lifecycle":
        if b.name in getattr(state, "compaction_status", {}):
            await _handle_provider_lifecycle(frontend, b, state, backend, body)
        else:
            await _project_progress(
                frontend, b, state, backend, "lifecycle", body, now=now
            )
        return

    if kind == "attachment":
        await _send_html_with_outbound_attachments(frontend, b, body)
        return

    if kind == "interaction_request":
        await _finalize_progress(
            frontend,
            b,
            state,
            backend,
            display_state="waiting",
            heading="🟠 <b>等待用户输入</b>",
        )
        await _send_attention(
            frontend, b, state, "🟠 <b>需要老板处理</b>", body
        )
        return

    if kind == "provider_error":
        await _finalize_progress(
            frontend,
            b,
            state,
            backend,
            display_state="error",
            heading="🔴 <b>过程失败</b>",
        )
        await _send_attention(
            frontend, b, state, "🔴 <b>任务未能继续</b>", body
        )
        getattr(state, "admin_delivery_contexts", {}).pop(b.name, None)
        return

    if kind == "assistant_plan":
        await _project_progress(frontend, b, state, backend, "plan", body, now=now)
        return

    if kind == "assistant_live_text":
        log.info(f"[{b.name}] assistant live text buffered len={len(body)}")
        _buffer_result_draft(state, b, body, replace=True)
        return

    if kind == "assistant_text_delta":
        log.info(f"[{b.name}] assistant text delta buffered len={len(body)}")
        _buffer_result_draft(state, b, body)
        return

    if kind == "assistant_text":
        log.info(f"[{b.name}] assistant final text len={len(body)}")
        # Final text closes both a pre-upgrade legacy aggregator and the single
        # progress projection before publishing a separate result notification.
        await _close_aggregator(b, state, frontend, backend)
        await _finalize_progress(frontend, b, state, backend)
        # ★ 真说话 → 单独发新消息触发 TG 通知, 不动 aggregator
        # 剥掉 claude 手写 footer + 从 harness 任务文件渲染任务 footer 追加 (§6)
        text = strip_handwritten_footer(body)
        out = _append_footer(text, _task_footer(b, backend))
        if out.strip():
            if _result_already_published(state, b, out):
                _audit(state, b, "duplicate_results_suppressed")
                return
            await _send_assistant_reply(frontend, b, out, backend)
            _remember_published_result(state, b, out)
            _audit(state, b, "results_published")
            _audit(state, b, "result_body_chars", len(out))
        _clear_result_draft(state, b)
        getattr(state, "admin_delivery_contexts", {}).pop(b.name, None)
        return

    if kind != "assistant_tools":
        # 未知 kind, 兜底直发
        await _send_html_with_outbound_attachments(frontend, b, body)
        return

    clean_body, attachments = split_outbound_attachments(body, cwd=b.cwd)
    if clean_body.strip():
        progress_kind = "error" if "失败" in clean_body or "⚠️" in clean_body else "tool"
        await _project_progress(
            frontend, b, state, backend, progress_kind, clean_body, now=now
        )
    await _send_outbound_attachments(frontend, b, attachments)


async def _handle_provider_lifecycle(frontend, b, state, backend, body: str) -> None:
    current = state.compaction_status.pop(b.name, None)
    footer = _task_footer(b, backend)
    content = _append_footer(body, footer)
    if current and current.get("msg_id") is not None:
        await frontend.finalize_status_html(
            current["chat_id"],
            current["msg_id"],
            content,
            display_state="completed",
        )
    else:
        await frontend.send_status_html(
            b.chat_id,
            b.thread_id,
            content,
            display_state="completed",
            footer=await _capture_terminal_status(b, backend),
        )


def _task_footer(b: "Binding", backend: "Backend") -> str:
    task_footer = render_task_footer(
        backend.read_tasks(b),
        style="pi" if backend.name == "pi" else "summary",
    )
    render_extension = getattr(backend, "render_extension_footer", None)
    extension_footer = render_extension(b) if callable(render_extension) else ""
    return "\n\n".join(part for part in (extension_footer, task_footer) if part)


def _append_footer(body: str, footer: str) -> str:
    return f"{body.rstrip()}\n\n{footer}" if footer else body.rstrip()


async def _send_attention(
    frontend: "Frontend", b: "Binding", state: "State", heading: str, html_text: str,
) -> None:
    await frontend.send_html(
        b.chat_id, b.thread_id, f"{heading}\n{html_text.strip()}"
    )
    _audit(state, b, "attention_published")


async def _send_html_with_outbound_attachments(
    frontend: "Frontend", b: "Binding", html_text: str,
) -> None:
    clean_text, attachments = split_outbound_attachments(html_text, cwd=b.cwd)
    if clean_text.strip():
        await frontend.send_html(b.chat_id, b.thread_id, clean_text)
    await _send_outbound_attachments(frontend, b, attachments)


async def _send_assistant_reply(
    frontend: "Frontend", b: "Binding", html_text: str, backend: "Backend",
) -> None:
    clean_text, attachments = split_outbound_attachments(html_text, cwd=b.cwd)
    status = await _capture_terminal_status(b, backend)
    if status is None:
        display_state = "completed"
    elif status.state.value in {"blocked", "dead"}:
        display_state = "error"
    elif status.state.value == "waiting":
        display_state = "waiting"
    else:
        display_state = "completed"
    envelope = ReplyEnvelope(
        title="回复",
        body=clean_text,
        footer=status,
        attachments=tuple(str(a.path) for a in attachments),
        actions=("screen", "status", "cancel", "interrupt"),
        metadata={"display_state": display_state},
    )
    await frontend.send_assistant_reply(b, envelope)


async def _capture_terminal_status(
    b: "Binding", backend: "Backend",
) -> TerminalStatus | None:
    """Capture runtime state and enrich it with one provider metadata snapshot."""
    try:
        pane = await asyncio.to_thread(tmux_capture, b.tmux_target, 30)
        status = backend.parse_terminal_status(pane)
        status_enricher = getattr(backend, "enrich_terminal_status", None)
        if callable(status_enricher):
            status = status_enricher(b, status)
        metadata_getter = getattr(backend, "current_runtime_metadata", None)
        metadata = (
            metadata_getter(b)
            if callable(metadata_getter)
            else ProviderRuntimeMetadata()
        )
    except Exception:
        log.exception("[%s] provider status capture failed", b.name)
        return None
    if status is None:
        return TerminalStatus(
            state=TerminalState.IDLE,
            provider=metadata.provider,
            model=metadata.model,
            effort=metadata.effort,
            permission_mode=metadata.permission_mode,
            cwd=str(b.cwd),
            session_name=metadata.session_name,
            input_tokens=metadata.input_tokens,
            output_tokens=metadata.output_tokens,
            cache_read_tokens=metadata.cache_read_tokens,
            cache_write_tokens=metadata.cache_write_tokens,
            cache_hit_rate=metadata.cache_hit_rate,
            cost_usd=metadata.cost_usd,
        )
    # Transcript metadata is authoritative. TUI scrollback can contain a tool/subagent
    # label (for example ``claude-code-guide``) that looks like a model name.
    if any(
        (
            metadata.provider and status.provider != metadata.provider,
            metadata.model and status.model != metadata.model,
            metadata.effort and status.effort != metadata.effort,
            status.permission_mode is None and metadata.permission_mode,
            status.cwd is None,
            status.session_name is None and metadata.session_name,
            status.input_tokens is None and metadata.input_tokens is not None,
            status.output_tokens is None and metadata.output_tokens is not None,
            status.cache_read_tokens is None and metadata.cache_read_tokens is not None,
            status.cache_write_tokens is None and metadata.cache_write_tokens is not None,
            status.cache_hit_rate is None and metadata.cache_hit_rate is not None,
            status.cost_usd is None and metadata.cost_usd is not None,
        )
    ):
        return replace(
            status,
            provider=metadata.provider or status.provider,
            model=metadata.model or status.model,
            effort=metadata.effort or status.effort,
            permission_mode=status.permission_mode or metadata.permission_mode,
            cwd=status.cwd or str(b.cwd),
            session_name=status.session_name or metadata.session_name,
            input_tokens=(
                status.input_tokens
                if status.input_tokens is not None
                else metadata.input_tokens
            ),
            output_tokens=(
                status.output_tokens
                if status.output_tokens is not None
                else metadata.output_tokens
            ),
            cache_read_tokens=(
                status.cache_read_tokens
                if status.cache_read_tokens is not None
                else metadata.cache_read_tokens
            ),
            cache_write_tokens=(
                status.cache_write_tokens
                if status.cache_write_tokens is not None
                else metadata.cache_write_tokens
            ),
            cache_hit_rate=(
                status.cache_hit_rate
                if status.cache_hit_rate is not None
                else metadata.cache_hit_rate
            ),
            cost_usd=(
                status.cost_usd if status.cost_usd is not None else metadata.cost_usd
            ),
        )
    return status


def _buffer_result_draft(
    state: "State", b: "Binding", html_text: str, *, replace: bool = False,
) -> None:
    drafts = getattr(state, "result_drafts", None)
    if drafts is None:
        drafts = {}
        setattr(state, "result_drafts", drafts)
    current = drafts.get(b.name, "")
    drafts[b.name] = html_text if replace else current + html_text


def _clear_result_draft(state: "State", b: "Binding") -> None:
    getattr(state, "result_drafts", {}).pop(b.name, None)


def _result_already_published(state: "State", b: "Binding", html_text: str) -> bool:
    return getattr(state, "published_results", {}).get(b.name) == _normalize_live_text(
        html_text
    )


def _remember_published_result(state: "State", b: "Binding", html_text: str) -> None:
    published = getattr(state, "published_results", None)
    if published is None:
        published = {}
        setattr(state, "published_results", published)
    published[b.name] = _normalize_live_text(html_text)


def _normalize_live_text(html_text: str) -> str:
    return "\n".join(line.rstrip() for line in html_text.strip().splitlines())


async def _send_outbound_attachments(
    frontend: "Frontend", b: "Binding", attachments,
) -> None:
    for attachment in attachments:
        if attachment.kind == "image":
            await frontend.send_image(b.chat_id, b.thread_id, attachment.path)
        else:
            await frontend.send_file(b.chat_id, b.thread_id, attachment.path)
