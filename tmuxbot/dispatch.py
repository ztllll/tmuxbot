"""共享命令分发层: 前端无关的文本 → tmux 注入 / 命令处理。

两个前端 (Telegram / 飞书) 共用同一套命令逻辑:
  - stop 命令 (/esc /cc /eof): 发 tmux key, 不注入 claude
  - capture 类命令 (/context /cost /compact /clear /new 等 + backend 别名):
    剥 @botname 后缀 → 解析别名 → tmux_send_text → capture_and_push (background)
  - /screen: capture 屏幕 → frontend.send_pre
  - /info:   aggregate_usage 统计卡片 → frontend.send_html
  - /restart: C-c + C-d + ensure_running → frontend.send_html
  - /rename pending 态: 下一条文本作为名字
  - 普通文本: tmux_send_text 注入, 不 capture

TG 专属逻辑 (BotCommand 菜单 handler、@botname 剥离、m.reply、setup_mode)
保留在 telegram.py, 不移到这里。
"""
from __future__ import annotations

import asyncio
import html
import logging
import time
from typing import TYPE_CHECKING

from tmuxbot.command_adapter import (
    action_from_command,
    classify_command,
    handle_interactive_command,
    handle_passthrough_command,
    handle_semantic_action,
    handle_tui_action,
    parse_slash_text,
    semantic_action_from_command,
    CommandKind,
)
from tmuxbot.lifecycle import ensure_binding_running
from tmuxbot.tmux import tmux_capture, tmux_send_key, tmux_send_text

if TYPE_CHECKING:
    from tmuxbot.backends.base import Backend
    from tmuxbot.frontends.base import Frontend
    from tmuxbot.state import Binding, State

log = logging.getLogger("tmuxbot")

# stop 命令集: 这些命令不注入 claude, 直接操作 tmux key
_STOP_CMDS = frozenset({"/esc", "/cc", "/eof"})

# capture-only 命令集 (不走 command_opts 但也是命令行为): /screen /info /restart
_LOCAL_CMDS = frozenset({"/screen", "/info", "/restart"})


async def dispatch_incoming_text(
    frontend: "Frontend",
    backend: "Backend",
    b: "Binding",
    state: "State",
    chat_id: int | str,
    thread_id: int | None,
    text: str,
    *,
    bot_username: str | None = None,   # TG 专属: @bot_username 后缀剥离
) -> None:
    """前端无关的命令分发入口。

    Args:
        frontend:     当前前端实例 (send_html / send_pre 接口)
        backend:      当前后端实例 (command_opts / command_aliases / ensure_running 等)
        b:            目标 binding
        state:        全局 State (pending_rename / fire 等)
        chat_id:      目标 chat (TG int / 飞书 str)
        thread_id:    TG topic thread_id, 飞书传 None
        text:         原始消息文本 (TG 传 m.text, 飞书传解析后 text)
        bot_username: TG 在 group 内命令自动加 @bot_username 后缀 — 传入供剥离;
                      飞书不传 (None)
    """
    from tmuxbot.commands import capture_and_push

    await ensure_binding_running(backend, b, state, reason="incoming", wait=True)

    # ── /rename pending 态: 下一条文本作为名字 ──
    pending_ts = state.pending_rename.get(b.name)
    if pending_ts and (time.time() - pending_ts) < 120:
        state.pending_rename.pop(b.name, None)
        text_stripped = text.strip()
        if text_stripped in ("/esc", "/cc"):
            tmux_send_key(b.tmux_target, "Escape")
            await frontend.send_html(chat_id, thread_id, "⎋ <b>已取消 rename</b>")
            return
        await tmux_send_text(
            b.tmux_target,
            text,
            expected_commands=backend.running_command_names,
        )
        await frontend.send_html(
            chat_id, thread_id,
            f"✏️ <b>已提交新名字</b>: <code>{html.escape(text)}</code>",
        )
        return
    if pending_ts:
        state.pending_rename.pop(b.name, None)

    raw_text = text
    cmd_for_feedback: str | None = None
    parsed = parse_slash_text(
        text, bot_username=bot_username, aliases=backend.command_aliases()
    )

    if parsed:
        raw_text = parsed.injected_text
        cmd_for_feedback = parsed.command
        spec = classify_command(backend, parsed.command)

        action = action_from_command(parsed.command, parsed.args)
        if action:
            await handle_tui_action(frontend, b, chat_id, thread_id, action)
            return

        semantic_action = semantic_action_from_command(parsed.command)
        if semantic_action:
            await handle_semantic_action(frontend, b, chat_id, thread_id, semantic_action)
            return

        if spec.kind == CommandKind.BLOCKED:
            return await frontend.send_html(chat_id, thread_id, spec.notice)

        if spec.kind == CommandKind.INTERACTIVE:
            await handle_interactive_command(
                frontend, b, state, chat_id, thread_id, spec, raw_text
            )
            return

        if spec.kind == CommandKind.PASSTHROUGH:
            await handle_passthrough_command(
                frontend, b, state, chat_id, thread_id, spec, raw_text
            )
            return

    # ── stop 命令: 发 tmux key, 不注入 claude ──
    if cmd_for_feedback in _STOP_CMDS:
        if cmd_for_feedback == "/esc":
            tmux_send_key(b.tmux_target, "Escape")
            await frontend.send_html(chat_id, thread_id, "⎋ Escape")
        elif cmd_for_feedback == "/cc":
            tmux_send_key(b.tmux_target, "C-c")
            await frontend.send_html(chat_id, thread_id, "⌃C")
        elif cmd_for_feedback == "/eof":
            tmux_send_key(b.tmux_target, "C-d")
            await frontend.send_html(chat_id, thread_id, "⌃D")
        return

    # ── /screen: capture 屏幕 ──
    if cmd_for_feedback == "/screen":
        out = tmux_capture(b.tmux_target, 60)
        await frontend.send_pre(chat_id, thread_id, out)
        return

    # ── /info: aggregate_usage 统计卡片 ──
    if cmd_for_feedback == "/info":
        jl = backend.find_active_jsonl(b)
        if not jl:
            await frontend.send_html(chat_id, thread_id, "📊 没找到 jsonl 文件")
            return
        stats = backend.aggregate_usage(jl, last_n=500)
        if not stats:
            await frontend.send_html(chat_id, thread_id, "📊 jsonl 里还没有 assistant 数据")
            return

        def _fmt(n: int) -> str:
            return f"{n:,}"

        total_in = stats["input"] + stats["cache_create"] + stats["cache_read"]
        parts = [
            f"📊 <b>会话累计统计</b>  · {html.escape(str(b.name))}",
            f"📨 助手回复 <b>{stats['count']}</b> 条",
        ]
        if stats.get("model"):
            parts.append(f"🧠 当前模型 <code>{html.escape(stats['model'])}</code>")
        parts += [
            "",
            f"📥 计费输入合计 <code>{_fmt(total_in)}</code>",
            f"   ├ 新输入 <code>{_fmt(stats['input'])}</code>",
            f"   ├ 缓存创建 <code>{_fmt(stats['cache_create'])}</code>",
            f"   └ 缓存命中 <code>{_fmt(stats['cache_read'])}</code>",
            f"📤 输出 token <code>{_fmt(stats['output'])}</code>",
            "",
            f"🎯 <b>缓存命中率 {stats['cache_hit_rate'] * 100:.1f}%</b>",
        ]
        if stats.get("last_ts"):
            parts.append(f"⏱ 最近回复 <code>{html.escape(stats['last_ts'])}</code>")
        await frontend.send_html(chat_id, thread_id, "\n".join(parts))
        return

    # ── /restart: C-c + C-d + ensure_running ──
    if cmd_for_feedback == "/restart":
        tmux_send_key(b.tmux_target, "C-c")
        await asyncio.sleep(0.5)
        tmux_send_key(b.tmux_target, "C-d")
        await asyncio.sleep(2.0)
        await ensure_binding_running(backend, b, state, reason="restart", wait=True)
        await frontend.send_html(chat_id, thread_id, f"🔄 已 restart {html.escape(backend.name)}")
        return

    # ── capture 类 slash 命令: 注入 + background capture_and_push ──
    if cmd_for_feedback and cmd_for_feedback in backend.command_opts():
        opts = backend.command_opts()[cmd_for_feedback]
        if opts.expect_new_session:
            # 仅允许本次通道命令之后创建的 transcript 接管 identity；避免同 cwd
            # 的其他 tmux binding 被“最新文件”规则误认领。
            b.pending_session_handoff_after = time.time()
        await tmux_send_text(
            b.tmux_target,
            raw_text,
            expected_commands=backend.running_command_names,
        )
        if cmd_for_feedback == "/rename":
            state.pending_rename[b.name] = time.time()
        state.fire(
            capture_and_push(frontend, b, backend, chat_id, thread_id, command=cmd_for_feedback)
        )
        return

    # ── 普通文本 (含未知命令): 直接注入, 不 capture ──
    await tmux_send_text(
        b.tmux_target,
        raw_text,
        expected_commands=backend.running_command_names,
    )
