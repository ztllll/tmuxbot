"""Detect live TUI interactions and notify the exact IM endpoint.

OMP interactions are observation-only: tmuxbot recognizes a live menu/input from
its current bottom controls, then asks the operator to SSH into the exact pane.
It never translates IM buttons or slash commands into OMP navigation keys.

Claude keeps its legacy numbered picker fallback because its transactional JSONL
can hide picker content from the channel entirely.
"""

from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from tmuxbot.tmux import find_omp_footer_pair, tmux_capture
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

_OMP_MENU_HINT_RE = re.compile(
    r"(?=.*\b(?:enter|return)\b\s+(?:to\s+)?(?:select|toggle|details|assign(?:\s+\w+)?)\b)"
    r"(?=.*\b(?:esc|escape)\b\s+(?:to\s+)?(?:close|cancel|back)\b)",
    re.I,
)
_OMP_CONFIRM_HINT_RE = re.compile(
    r"(?=.*\b(?:enter|return)\b\s+(?:to\s+)?confirm\b)"
    r"(?=.*\b(?:esc|escape)\b\s+(?:to\s+)?(?:close|cancel|back)\b)",
    re.I,
)
_OMP_INPUT_HINT_RE = re.compile(
    r"(?=.*\b(?:enter|return)\b\s+(?:to\s+)?submit\b)"
    r"(?=.*\b(?:esc|escape)\b\s+(?:to\s+)?(?:close|cancel|back)\b)",
    re.I,
)
_OMP_SELECTED_ROW_RE = re.compile(r"^\s*(?:│\s*)?[→›❯]\s+\S", re.M)
_OMP_NAVIGATION_RE = re.compile(r"(?:↑/↓|up/down|up\s*/\s*down)", re.I)
_OMP_HINT_END_RE = re.compile(r"\b(?:esc|escape)\b\s+(?:to\s+)?(?:close|cancel|back)\b", re.I)
_OMP_MODAL_TOP_RE = re.compile(r"^\s*╭[─━].*[─━]╮\s*$")
_OMP_MODAL_BOTTOM_RE = re.compile(r"^\s*╰[─━].*[─━]╯\s*$")


@dataclass(frozen=True, slots=True)
class OmpInteraction:
    kind: str
    label: str
    block: str


def detect_omp_interaction(raw: str) -> OmpInteraction | None:
    """Return only a current OMP footer-adjacent view or bottom-most modal."""
    clean = strip_decorations(raw)
    lines = clean.splitlines()
    pair = find_omp_footer_pair(lines)
    modal_top: int | None = None

    if pair is not None:
        footer_index, _ = pair
        nonempty_before: list[int] = []
        for index in range(footer_index - 1, -1, -1):
            if lines[index].strip():
                nonempty_before.append(index)
                if len(nonempty_before) == 3:
                    break
        if not nonempty_before:
            return None
        hint_indices = sorted(nonempty_before)
    else:
        nonempty = [index for index, line in enumerate(lines) if line.strip()]
        if len(nonempty) < 3 or not _OMP_MODAL_BOTTOM_RE.fullmatch(lines[nonempty[-1]]):
            return None
        hint_indices = [nonempty[-2]]
        for index in range(nonempty[-2] - 1, -1, -1):
            if _OMP_MODAL_TOP_RE.fullmatch(lines[index]):
                modal_top = index
                break
        if modal_top is None:
            return None

    nearest_line = lines[hint_indices[-1]].strip()
    if _OMP_HINT_END_RE.search(nearest_line) is None:
        return None
    hint_text = " ".join(lines[index].strip() for index in hint_indices)
    is_input = bool(_OMP_INPUT_HINT_RE.search(hint_text))
    is_confirmation = bool(_OMP_CONFIRM_HINT_RE.search(hint_text))
    is_menu = bool(_OMP_MENU_HINT_RE.search(hint_text))
    if not (is_input or is_confirmation or is_menu):
        return None

    bottom_index = hint_indices[-1]
    top_index = modal_top if modal_top is not None else 0
    if modal_top is None:
        blank_run = 0
        for index in range(hint_indices[0] - 1, -1, -1):
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
    if is_input:
        return OmpInteraction(kind="text_input", label="文本输入", block=block)
    if is_confirmation:
        return OmpInteraction(kind="confirmation", label="确认界面", block=block)
    if is_menu and (_OMP_SELECTED_ROW_RE.search(block) or _OMP_NAVIGATION_RE.search(hint_text)):
        return OmpInteraction(kind="selection", label="选择菜单", block=block)
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

    backend = frontend.backend_for(b)
    if not backend.remote_tui_actions_allowed:
        interaction = detect_omp_interaction(out)
        if interaction is None:
            state.picker_notified.pop(b.name, None)
            return
        fingerprint = f"native:{interaction.kind}:{hash(interaction.block)}"
        if state.picker_notified.get(b.name) == fingerprint:
            return
        state.picker_notified[b.name] = fingerprint
        text = backend.format_remote_interaction_notice(b, interaction.label)
        if text is None:
            return
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
