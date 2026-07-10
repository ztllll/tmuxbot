"""slash 命令兜底: capture_and_push + inject_slash_and_capture。

UI/picker 类命令 (/context /cost /compact 等) 不写 jsonl, 注入后等屏幕稳定 →
- 优先用 backend 提供的 parser 出结构化摘要
- 没命中走 fallback_summary
- 都没用就发原始屏幕 (strip_decorations)
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from tmuxbot.backends.base import CmdOpts
from tmuxbot.tmux import tmux_capture, tmux_send_key, tmux_send_text
from tmuxbot.utils import strip_decorations

if TYPE_CHECKING:
    from tmuxbot.backends.base import Backend
    from tmuxbot.frontends.base import Frontend
    from tmuxbot.state import Binding

log = logging.getLogger("tmuxbot")


async def inject_slash_and_capture(
    b: "Binding", cmd: str, *, backend: "Backend | None" = None,
    settle_iters: int = 12, poll: float = 0.4,
) -> str:
    """注入 slash 命令 → 等屏稳定 hash 2 次 → capture → Esc 退 modal → 返回 raw 屏"""
    expected = backend.running_command_names if backend is not None else None
    await tmux_send_text(
        b.tmux_target,
        cmd,
        with_enter=True,
        expected_commands=expected,
    )
    last_hash, stable, out = "", 0, ""
    for _ in range(settle_iters):
        await asyncio.sleep(poll)
        out = tmux_capture(b.tmux_target, 100)
        h = str(hash(out))
        if h == last_hash:
            stable += 1
            if stable >= 2:
                break
        else:
            stable, last_hash = 0, h
    tmux_send_key(b.tmux_target, "Escape")
    await asyncio.sleep(0.15)
    return out


def _fmt_k(n: int | None) -> str:
    """1234567 → '1.2m', 12345 → '12.3k', 234 → '234'"""
    if n is None:
        return "?"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}m"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def _fmt_token_delta(before: int | None, after: int | None) -> str | None:
    """压缩前后 token 对比 → '📉 880k → 22k (压缩 97%)'。任一为 None 返回 None。"""
    if before is None or after is None or before <= 0:
        return None
    if after == before:
        return f"· token 未变化 <code>{_fmt_k(before)}</code>"
    if after < before:
        pct = round((before - after) / before * 100)
        return f"📉 token <code>{_fmt_k(before)}</code> → <code>{_fmt_k(after)}</code> (压缩 {pct}%)"
    pct = round((after - before) / before * 100)
    return f"📈 token <code>{_fmt_k(before)}</code> → <code>{_fmt_k(after)}</code> (增加 {pct}%)"


async def capture_and_push(
    frontend: "Frontend", b: "Binding", backend: "Backend",
    chat_id: int, thread_id: int | None,
    *,
    command: str | None = None,
) -> None:
    """slash 命令兜底: 等屏幕稳定 + 3 档兜底 (parser / fallback / raw)。

    ★ /compact /clear /new 等 expect_new_session 命令: jsonl 切换 (硬信号) 优先于
    屏幕 done_pattern (软信号) — 屏幕历史里残留的老 'Compacted' 字样会假阳。
    """
    key = (command or "").lstrip().split()[0] if command else ""
    opts: CmdOpts = backend.command_opts().get(key, CmdOpts())
    initial_session = b.last_session_id

    # /clear /new: 入口拿压缩前 ctx size (走 read_context_size); /compact: 入口锁
    # jsonl 字节数, 用于 compact_boundary marker 的范围限定 (token 从 metadata 拿)
    before_size: int | None = None
    before_jsonl = None
    before_jsonl_size: int = 0
    if opts.expect_new_session or opts.expect_compact_done:
        try:
            before_jsonl = backend.find_active_jsonl(b)
            if before_jsonl and before_jsonl.is_file():
                before_jsonl_size = before_jsonl.stat().st_size
            if opts.expect_new_session:
                before_size = backend.read_context_size(before_jsonl)
        except Exception as e:
            log.debug(f"capture_and_push {key} read before context: {e}")

    notice_msg = None
    if opts.notice:
        try:
            notice_msg = await frontend.send_html(chat_id, thread_id, opts.notice)
        except Exception:
            pass

    await asyncio.sleep(opts.init_delay)
    last_hash, stable, out = "", 0, ""
    summary: str | None = None
    new_session_seen = False
    compact_meta: dict | None = None
    early_reason = "max_iters"

    for i in range(opts.max_iters):
        out = tmux_capture(b.tmux_target, opts.lines)
        # ★ 硬信号 1: jsonl session 切换 — /clear /new 真触发会换 session_id 新建 jsonl
        if (
            opts.expect_new_session
            and initial_session
            and b.last_session_id
            and b.last_session_id != initial_session
        ):
            new_session_seen = True
            early_reason = "session_switch"
            break
        # ★ 硬信号 2: jsonl 末尾出现 compact_boundary system event — /compact 不切 session 但写 marker
        if opts.expect_compact_done:
            try:
                cur_jsonl = backend.find_active_jsonl(b)
                meta = backend.compact_metadata_since(cur_jsonl, before_jsonl_size)
                if meta is not None:
                    compact_meta = meta
                    early_reason = "compact_boundary"
                    break
            except Exception as e:
                log.debug(f"capture_and_push {key} compact_metadata_since err: {e}")
        # done_pattern 仅对不要求 jsonl 硬信号的命令用 (避免屏幕历史里残留字样假阳)
        if (
            not opts.expect_new_session
            and not opts.expect_compact_done
            and opts.done_pattern
            and opts.done_pattern.search(strip_decorations(out))
        ):
            early_reason = "done_pattern"
            break
        if (
            opts.parser and opts.parser_can_retry
            and not opts.expect_new_session
            and not opts.expect_compact_done
        ):
            try:
                s = opts.parser(out)
            except Exception as e:
                log.debug(f"parser {key} err: {e}")
                s = None
            if s:
                summary = s
                early_reason = "parser_hit"
                break
        # stable 早退: 仅对无 hard signal 的命令; expect_new_session / expect_compact_done
        # 已有 jsonl 硬信号兜底, 屏幕静止 ≠ jsonl flush 完成 (claude TUI 事务式 flush —
        # 屏幕显示 'Compacted' 字样会比 compact_boundary marker 落盘早 30-120s)
        if not opts.expect_new_session and not opts.expect_compact_done:
            h = str(hash(out))
            if h == last_hash:
                stable += 1
                if stable >= 2:
                    early_reason = "stable"
                    break
            else:
                stable, last_hash = 0, h
        await asyncio.sleep(opts.poll)

    log.info(f"capture_and_push {key} done: {early_reason} (iter ~{i + 1})")

    # 循环出口后再补一次 session_switch 检测 (jsonl tailer 可能在 sleep 间刚好刷上)
    if (
        not new_session_seen
        and opts.expect_new_session
        and initial_session
        and b.last_session_id
        and b.last_session_id != initial_session
    ):
        new_session_seen = True
    if compact_meta is None and opts.expect_compact_done:
        # jsonl flush 是事务式的, marker 落盘可能比循环退出晚 — 5 次 × 1s 重试兜底
        for retry in range(5):
            try:
                compact_meta = backend.compact_metadata_since(
                    backend.find_active_jsonl(b), before_jsonl_size,
                )
            except Exception as e:
                log.debug(f"capture_and_push {key} final compact check (retry {retry}): {e}")
            if compact_meta is not None:
                log.info(f"capture_and_push {key} compact_meta hit on retry {retry}")
                break
            await asyncio.sleep(1.0)

    # /clear /new: 新 session 后 jsonl 已切换, 拿 after_size 算压缩 delta
    # /compact: 直接读 compactMetadata.pre/postTokens, 不走 read_context_size
    delta_line: str | None = None
    compact_extra_line: str | None = None
    if opts.expect_new_session and new_session_seen:
        try:
            after_jsonl = backend.find_active_jsonl(b)
            after_size = backend.read_context_size(after_jsonl)
            delta_line = _fmt_token_delta(before_size, after_size)
        except Exception as e:
            log.debug(f"capture_and_push {key} read after context: {e}")
    if opts.expect_compact_done and compact_meta:
        delta_line = _fmt_token_delta(compact_meta.get("preTokens"), compact_meta.get("postTokens"))
        dur_ms = compact_meta.get("durationMs")
        trig = compact_meta.get("trigger") or "?"
        if dur_ms:
            compact_extra_line = f"⏱ 耗时 <code>{dur_ms / 1000:.1f}s</code> · 触发 <code>{trig}</code>"

    try:
        # 末尾再调一次 parser (覆盖 /clear /new 等固定文案 parser)
        if summary is None and opts.parser and out.strip():
            try:
                summary = opts.parser(out)
            except Exception:
                log.exception(f"parser {key} err")

        if summary:
            if new_session_seen and b.last_session_id:
                import html as _html
                summary += f"\n· 新会话 <code>{_html.escape(b.last_session_id[:8])}</code>"
            if delta_line:
                summary += f"\n{delta_line}"
            if compact_extra_line:
                summary += f"\n{compact_extra_line}"
            # /status 补「跟上游无关、两端通用」的综合信息 (上下文/缓存/token, 读 jsonl)
            # → 直连/中转两端 /status 核心内容一致, 只有配额(OAuth)因接口差异中转端省略
            if key == "/status":
                try:
                    summary += backend.status_extra(b)
                except Exception:
                    log.exception(f"status_extra err for {b.name}")
            await frontend.send_html(chat_id, thread_id, summary)
            return
        total_wait = opts.init_delay + opts.max_iters * opts.poll + 5  # +5s for final retry
        if opts.expect_new_session and not new_session_seen:
            warn = f"⚠️ <b>{key} 未确认完成</b>\n· jsonl 在 {total_wait:.0f}s 内未切换 (命令可能未真触发, 检查 TUI 屏幕)"
            await frontend.send_html(chat_id, thread_id, warn)
            return
        if opts.expect_compact_done and compact_meta is None:
            warn = f"⚠️ <b>{key} 未确认完成</b>\n· jsonl 在 {total_wait:.0f}s 内未出现 compact_boundary marker (命令可能未真触发, 检查 TUI 屏幕)"
            await frontend.send_html(chat_id, thread_id, warn)
            return
        if opts.fallback_summary:
            fb = opts.fallback_summary
            if new_session_seen and b.last_session_id:
                import html as _html
                fb += f"\n· 新会话 <code>{_html.escape(b.last_session_id[:8])}</code>"
            if delta_line:
                fb += f"\n{delta_line}"
            if compact_extra_line:
                fb += f"\n{compact_extra_line}"
            await frontend.send_html(chat_id, thread_id, fb)
            return
        if out.strip():
            cleaned = strip_decorations(out)
            if cleaned:
                await frontend.send_pre(chat_id, thread_id, cleaned)
    finally:
        if not opts.expect_new_session and not opts.expect_compact_done:
            try:
                tmux_send_key(b.tmux_target, "Escape")
            except Exception:
                pass
        if notice_msg is not None:
            try:
                await notice_msg.delete()
            except Exception:
                pass
