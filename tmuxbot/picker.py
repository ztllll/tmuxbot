"""Picker 检测 + 屏幕原文推送。

claude TUI 弹 picker 时 jsonl 事务式 buffer 不可见, bot 端只能屏幕 OCR 兜底:
- PICKER_BOTTOMBAR_RE 严格识别底栏 (Enter to select + ↑/↓ + Esc to cancel 同一行)
- extract_picker_block 抓 picker 字符画块
- detect_idle_picker 推 <pre> + 1-9 数字按钮到 TG
"""
from __future__ import annotations

import html
import logging
import re
from typing import TYPE_CHECKING

from tmuxbot.tmux import tmux_capture
from tmuxbot.utils import strip_decorations

if TYPE_CHECKING:
    from tmuxbot.frontends.base import Frontend
    from tmuxbot.state import Binding, State

log = logging.getLogger("tmuxbot")

# picker 底栏: 3 个关键词必须同一行 (避免命中代码注释/历史对话残留)
PICKER_BOTTOMBAR_RE = re.compile(
    r"Enter\s+to\s+select[^\n]{1,120}(?:↑/↓|to\s+navigate)[^\n]{1,120}Esc\s+to\s+cancel",
    re.I,
)


def extract_picker_block(raw: str) -> str | None:
    """抓 picker 在屏幕上的原始字符画块 (从底栏向上扫到第一个空行边界)"""
    clean = strip_decorations(raw)
    if not PICKER_BOTTOMBAR_RE.search(clean):
        return None
    lines = clean.splitlines()
    bottom_idx = -1
    for i in range(len(lines) - 1, -1, -1):
        if PICKER_BOTTOMBAR_RE.search(lines[i]):
            bottom_idx = i
            break
    if bottom_idx < 0:
        return None
    # 向上扫边界: 遇 2 连空行就收
    top_idx = 0
    blank_run = 0
    for i in range(bottom_idx - 1, -1, -1):
        if not lines[i].strip():
            blank_run += 1
            if blank_run >= 2:
                top_idx = i + 2
                break
        else:
            blank_run = 0
    block_lines = [ln for ln in lines[top_idx:bottom_idx + 1] if ln.rstrip()]
    if not block_lines:
        return None
    return "\n".join(block_lines)


async def detect_idle_picker(b: "Binding", state: "State", frontend: "Frontend") -> None:
    """jsonl 长时间没动 + 屏幕有 picker → 推屏幕原文 + 1-9 按钮"""
    try:
        out = tmux_capture(b.tmux_target, 80)
    except Exception as e:
        log.debug(f"[{b.name}] picker capture err: {e}")
        return
    block = extract_picker_block(out)
    if block is None:
        state.picker_notified.pop(b.name, None)
        return
    h = str(hash(block))
    if state.picker_notified.get(b.name) == h:
        return
    state.picker_notified[b.name] = h

    body = block[:3000]
    text = (
        "⚠️ <b>TUI 有 picker 待响应</b>\n"
        "(claude jsonl 事务式 flush 中,完整卡片要等)\n\n"
        f"<pre>{html.escape(body)}</pre>\n"
        "<i>下方 1-9 按钮 = 模拟 ↓×N + Enter</i>"
    )
    log.info(f"[{b.name}] picker push ({len(block)} chars), hash={h[:8]}")
    try:
        await frontend.send_picker_card(b.chat_id, b.thread_id, text, b.name, num_options=9)  # type: ignore[attr-defined]
    except Exception:
        log.exception("picker push err")
