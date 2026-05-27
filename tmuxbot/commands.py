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
    b: "Binding", cmd: str, *, settle_iters: int = 12, poll: float = 0.4,
) -> str:
    """注入 slash 命令 → 等屏稳定 hash 2 次 → capture → Esc 退 modal → 返回 raw 屏"""
    await tmux_send_text(b.tmux_target, cmd, with_enter=True)
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

    # /compact 等 expect_new_session 命令: 入口拿压缩前 context size, 完成后对比
    before_size: int | None = None
    before_jsonl = None
    if opts.expect_new_session:
        try:
            before_jsonl = backend.find_active_jsonl(b)
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
    early_reason = "max_iters"

    for i in range(opts.max_iters):
        out = tmux_capture(b.tmux_target, opts.lines)
        # ★ session_switch 硬信号优先 — /compact /clear /new 必须确认 jsonl 已切换
        if (
            opts.expect_new_session
            and initial_session
            and b.last_session_id
            and b.last_session_id != initial_session
        ):
            new_session_seen = True
            early_reason = "session_switch"
            break
        # done_pattern 仅对不要求新会话的命令认定 (避免屏幕历史里老 Compacted 假阳)
        if (
            not opts.expect_new_session
            and opts.done_pattern
            and opts.done_pattern.search(strip_decorations(out))
        ):
            early_reason = "done_pattern"
            break
        if opts.parser and opts.parser_can_retry and not opts.expect_new_session:
            try:
                s = opts.parser(out)
            except Exception as e:
                log.debug(f"parser {key} err: {e}")
                s = None
            if s:
                summary = s
                early_reason = "parser_hit"
                break
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

    if (
        not new_session_seen
        and opts.expect_new_session
        and initial_session
        and b.last_session_id
        and b.last_session_id != initial_session
    ):
        new_session_seen = True

    # /compact 等: 切换后拿压缩后 context size, 算 delta
    delta_line: str | None = None
    if opts.expect_new_session and new_session_seen:
        try:
            after_jsonl = backend.find_active_jsonl(b)
            after_size = backend.read_context_size(after_jsonl)
            delta_line = _fmt_token_delta(before_size, after_size)
        except Exception as e:
            log.debug(f"capture_and_push {key} read after context: {e}")

    try:
        # 末尾再调一次 parser (覆盖 /clear /new 等固定文案 parser; 不会触发屏幕历史
        # 假阳, 因为我们已经先确认 session_switch 或者非 expect_new_session)
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
            await frontend.send_html(chat_id, thread_id, summary)
            return
        if opts.expect_new_session and not new_session_seen:
            # 关键: 没等到 jsonl 切换 = /compact 大概率没真触发, 报警告而非假阳成功
            warn = f"⚠️ <b>{key} 未确认完成</b>\n· jsonl 在 {opts.max_iters * opts.poll:.0f}s 内未切换 (命令可能未真触发, 检查 TUI 屏幕)"
            await frontend.send_html(chat_id, thread_id, warn)
            return
        if opts.fallback_summary:
            fb = opts.fallback_summary
            if new_session_seen and b.last_session_id:
                import html as _html
                fb += f"\n· 新会话 <code>{_html.escape(b.last_session_id[:8])}</code>"
            if delta_line:
                fb += f"\n{delta_line}"
            await frontend.send_html(chat_id, thread_id, fb)
            return
        if out.strip():
            cleaned = strip_decorations(out)
            if cleaned:
                await frontend.send_pre(chat_id, thread_id, cleaned)
    finally:
        if not opts.expect_new_session:
            try:
                tmux_send_key(b.tmux_target, "Escape")
            except Exception:
                pass
        if notice_msg is not None:
            try:
                await notice_msg.delete()
            except Exception:
                pass
