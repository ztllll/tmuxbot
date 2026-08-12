"""Detect live TUI interactions and notify the exact IM endpoint.

Pi interactions are observation-only: tmuxbot recognizes a live menu/input from
its current bottom controls, then asks the operator to SSH into the exact pane.
It never translates IM buttons or slash commands into Pi navigation keys.

Claude keeps its legacy numbered picker fallback because its transactional JSONL
can hide picker content from the channel entirely.
"""
from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from tmuxbot.runtime.pi_interaction import pi_ssh_interaction_notice
from tmuxbot.tmux import tmux_capture
from tmuxbot.utils import strip_decorations

if TYPE_CHECKING:
    from tmuxbot.frontends.base import Frontend
    from tmuxbot.state import Binding, State

log = logging.getLogger("tmuxbot")

# Legacy Claude picker: all controls must be on one line to avoid transcript prose.
PICKER_BOTTOMBAR_RE = re.compile(
    r"Enter\s+to\s+select[^\n]{1,120}(?:↑/↓|to\s+navigate)"
    r"[^\n]{1,120}Esc\s+to\s+cancel",
    re.I,
)

# pi-tui-kit/native Pi controls render a compact live hint immediately above
# the provider footer.  Each class requires both a commit key and a cancellation
# key; selections additionally require a visible selected-row marker or explicit
# navigation controls.  This avoids treating ordinary prose as interaction.
_PI_KIT_MENU_HINT_RE = re.compile(
    r"(?=.*(?:↑/↓|up/down|up\s*/\s*down))"
    r"(?=.*\b(?:navigate|scroll)\b)"
    r"(?=.*\b(?:enter|return)\b\s+(?P<action>select|toggle|details|confirm))"
    r"(?=.*\b(?:esc|escape)\b\s+(?:close|cancel|back))",
    re.I,
)
_PI_NATIVE_SELECT_HINT_RE = re.compile(
    r"(?=.*\b(?:enter|return)\b\s+(?:to\s+)?select\b)"
    r"(?=.*\b(?:esc|escape)\b\s+(?:to\s+)?(?:close|cancel|back)\b)",
    re.I,
)
_PI_INPUT_HINT_RE = re.compile(
    r"(?=.*\b(?:enter|return)\b\s+(?:to\s+)?submit\b)"
    r"(?=.*\b(?:esc|escape)\b\s+(?:to\s+)?(?:close|cancel|back)\b)",
    re.I,
)
_PI_SELECTED_ROW_RE = re.compile(r"^\s*[→›]\s+\S", re.M)
_PI_HINT_END_RE = re.compile(
    r"\b(?:esc|escape)\b\s+(?:to\s+)?(?:close|cancel|back)\b", re.I
)
_PI_LIVE_FOOTER_RE = re.compile(
    r"(?:🪟\s*ctx\s+|(?:\d+(?:\.\d+)?%|\?)\s*/\s*\d+(?:\.\d+)?[kKmM]?\b)",
    re.I,
)


@dataclass(frozen=True, slots=True)
class PiInteraction:
    kind: str
    label: str
    block: str


def detect_pi_interaction(raw: str) -> PiInteraction | None:
    """Return a live Pi interaction only when controls touch the current footer.

    Historical menu text in scrollback is deliberately ignored.  The control
    hint must be within the three non-empty lines directly preceding the active
    Pi status footer.  Menus require navigation/commit/cancel controls; text
    inputs require submit/cancel controls.
    """
    clean = strip_decorations(raw)
    lines = clean.splitlines()
    footer_index = next(
        (
            index
            for index in range(len(lines) - 1, -1, -1)
            if _PI_LIVE_FOOTER_RE.search(lines[index])
        ),
        None,
    )
    if footer_index is None:
        return None
    # A stale footer in scrollback is not a live provider.  Current Pi renders
    # at most a few extension-status lines below its footer.
    if sum(bool(line.strip()) for line in lines[footer_index + 1 :]) > 3:
        return None

    nonempty_before: list[int] = []
    for index in range(footer_index - 1, -1, -1):
        if lines[index].strip():
            nonempty_before.append(index)
            if len(nonempty_before) == 3:
                break
    if not nonempty_before:
        return None
    hint_indices = sorted(nonempty_before)
    nearest_line = lines[max(hint_indices)].strip()
    if _PI_HINT_END_RE.search(nearest_line) is None:
        return None
    hint_text = " ".join(lines[index].strip() for index in hint_indices)
    kit_match = _PI_KIT_MENU_HINT_RE.search(hint_text)
    native_select = bool(_PI_NATIVE_SELECT_HINT_RE.search(hint_text))
    text_input = bool(_PI_INPUT_HINT_RE.search(hint_text))
    if kit_match is None and not native_select and not text_input:
        return None

    bottom_index = max(hint_indices)
    top_index = 0
    blank_run = 0
    for index in range(min(hint_indices) - 1, -1, -1):
        if not lines[index].strip():
            blank_run += 1
            if blank_run >= 2:
                top_index = index + 2
                break
        else:
            blank_run = 0
    block = "\n".join(
        line.rstrip() for line in lines[top_index : bottom_index + 1] if line.rstrip()
    )
    if text_input:
        return PiInteraction(kind="text_input", label="文本输入", block=block)
    if kit_match is not None:
        action = kit_match.group("action").lower()
        kind, label = {
            "select": ("selection", "选择菜单"),
            "toggle": ("multi_select", "多选菜单"),
            "confirm": ("confirmation", "确认界面"),
            "details": ("details", "详情浏览"),
        }[action]
        return PiInteraction(kind=kind, label=label, block=block)
    if native_select and _PI_SELECTED_ROW_RE.search(block):
        return PiInteraction(kind="selection", label="选择菜单", block=block)
    return None


def extract_picker_block(raw: str) -> str | None:
    """Extract a legacy Claude picker block from its strict bottom-bar marker."""
    clean = strip_decorations(raw)
    if not PICKER_BOTTOMBAR_RE.search(clean):
        return None
    lines = clean.splitlines()
    bottom_idx = next(
        (
            index
            for index in range(len(lines) - 1, -1, -1)
            if PICKER_BOTTOMBAR_RE.search(lines[index])
        ),
        -1,
    )
    if bottom_idx < 0:
        return None
    top_idx = 0
    blank_run = 0
    for index in range(bottom_idx - 1, -1, -1):
        if not lines[index].strip():
            blank_run += 1
            if blank_run >= 2:
                top_idx = index + 2
                break
        else:
            blank_run = 0
    block_lines = [line for line in lines[top_idx : bottom_idx + 1] if line.rstrip()]
    return "\n".join(block_lines) if block_lines else None


async def detect_idle_picker(b: "Binding", state: "State", frontend: "Frontend") -> None:
    """Notify once for one exact live picker fingerprint."""
    try:
        out = tmux_capture(b.tmux_target, 80)
    except Exception as exc:
        log.debug("[%s] picker capture err: %s", b.name, exc)
        return

    if b.backend == "pi":
        interaction = detect_pi_interaction(out)
        if interaction is None:
            state.picker_notified.pop(b.name, None)
            return
        fingerprint = f"pi:{interaction.kind}:{hash(interaction.block)}"
        if state.picker_notified.get(b.name) == fingerprint:
            return
        state.picker_notified[b.name] = fingerprint
        text = pi_ssh_interaction_notice(b, interaction_label=interaction.label)
        sender = lambda: frontend.send_html(b.chat_id, b.thread_id, text)
        size = len(interaction.block)
    else:
        block = extract_picker_block(out)
        if block is None:
            state.picker_notified.pop(b.name, None)
            return
        fingerprint = f"legacy:{hash(block)}"
        if state.picker_notified.get(b.name) == fingerprint:
            return
        state.picker_notified[b.name] = fingerprint
        body = block[:3000]
        text = (
            "⚠️ <b>TUI 有 picker 待响应</b>\n\n"
            f"<pre>{html.escape(body)}</pre>\n"
            "<i>下方 1-9 按钮 = 模拟 ↓×N + Enter</i>"
        )
        sender = lambda: frontend.send_picker_card(
            b.chat_id, b.thread_id, text, b.name, num_options=9
        )
        size = len(block)

    log.info("[%s] picker notice (%d chars), fingerprint=%s", b.name, size, fingerprint[:24])
    try:
        await sender()
    except Exception:
        log.exception("picker notice failed")
