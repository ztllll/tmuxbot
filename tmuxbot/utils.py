"""通用工具:路径编码、ANSI 装饰清理、CJK 等宽渲染、offsets 持久化。"""
from __future__ import annotations

import html
import json
import logging
import re
import time
from pathlib import Path

log = logging.getLogger("tmuxbot")

# ────────── 任务进度 footer (bot 从 claude TodoWrite 状态渲染) ──────────
# 全局宪法 §6: Boss 远程看不到 TUI 任务列表 → bot 把 claude 的真实 TodoWrite
# 状态渲染成 footer 追加到推送。claude 只管维护 TodoWrite, 不手写 footer。
_HANDWRITTEN_FOOTER_RE = re.compile(r"\n*━━━\s*任务\s*━━━.*$", re.S)


def strip_handwritten_footer(text: str) -> str:
    """剥掉 claude 手写的 "━━━ 任务 ━━━ …" 块 (到文末)。
    防止旧习惯手写 footer 与 bot 渲染的 footer 重复 / 显示编造的假任务数。"""
    return _HANDWRITTEN_FOOTER_RE.sub("", text).rstrip()


def render_task_footer(todos: "list | None") -> str:
    """claude TodoWrite todos → §6 格式的任务 footer (HTML)。无任务返回 ""(不渲染)。

    todo item: {"content": str, "status": "pending|in_progress|completed", ...}
    格式: ◼ in_progress(加粗) · ◻ pending · ✓ <s>completed</s>(最早3个, 其余折叠)
    """
    if not todos:
        return ""
    done = [t for t in todos if t.get("status") == "completed"]
    in_prog = [t for t in todos if t.get("status") == "in_progress"]
    pending = [t for t in todos if t.get("status") == "pending"]
    n = len(todos)
    lines = [
        "━━━ 任务 ━━━",
        f"{n} tasks ({len(done)} done, {len(in_prog)} in progress, {len(pending)} open)",
    ]
    for t in in_prog:
        lines.append(f"◼ <b>{html.escape(str(t.get('content', '')))}</b>")
    for t in pending:
        lines.append(f"◻ {html.escape(str(t.get('content', '')))}")
    for t in done[:3]:
        lines.append(f"✓ <s>{html.escape(str(t.get('content', '')))}</s>")
    if len(done) > 3:
        lines.append(f"… +{len(done) - 3} completed")
    return "\n".join(lines)

# ────────── 路径编码 ──────────
def encode_cwd(p: Path) -> str:
    """claude / codex 的真实规则: 把绝对路径里所有非 [A-Za-z0-9] 字符替换为 -
    (/ . _ 空格 中文 等全部换)。中文项目目录的 jsonl 目录名才能跟 claude 算的一致。"""
    return re.sub(r"[^A-Za-z0-9]", "-", str(Path(p).resolve()))


# ────────── 长度计算 ──────────
def utf16_len(s: str) -> int:
    """Telegram 字符长度按 UTF-16 单位计 (4096 上限)"""
    return len(s.encode("utf-16-le")) // 2


# ────────── ANSI / TUI 装饰清理 ──────────
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
GRID_CHARS = "⛁⛀⛶█▓▒░"


def strip_decorations(text: str) -> str:
    """去 ANSI / 网格装饰字符 / 压缩空行,保留关键文本"""
    text = ANSI_RE.sub("", text)
    text = re.sub(rf"[{GRID_CHARS}]\s*", "", text)
    text = re.sub(r"\n[ \t]*\n[ \t]*\n+", "\n\n", text)
    text = "\n".join(line.rstrip() for line in text.splitlines())
    return text.strip()


# ────────── CJK 等宽渲染 ──────────
def cwidth(s: str) -> int:
    """字符显示宽度: CJK / 全角 / 部分 emoji 占 2 列, 其余 1 列"""
    w = 0
    for ch in s:
        cp = ord(ch)
        if cp < 0x20 or cp == 0x7F:
            continue
        if (
            0x1100 <= cp <= 0x115F
            or 0x2E80 <= cp <= 0x303E
            or 0x3041 <= cp <= 0x33FF
            or 0x3400 <= cp <= 0x4DBF
            or 0x4E00 <= cp <= 0x9FFF
            or 0xA000 <= cp <= 0xA4CF
            or 0xAC00 <= cp <= 0xD7A3
            or 0xF900 <= cp <= 0xFAFF
            or 0xFE30 <= cp <= 0xFE4F
            or 0xFF00 <= cp <= 0xFF60
            or 0xFFE0 <= cp <= 0xFFE6
            or 0x20000 <= cp <= 0x2FFFD
            or 0x30000 <= cp <= 0x3FFFD
            or 0x1F300 <= cp <= 0x1F9FF
            or 0x2600 <= cp <= 0x27BF
        ):
            w += 2
        else:
            w += 1
    return w


def cpad(s: str, width: int) -> str:
    """右补空格到目标显示宽度"""
    return s + " " * max(0, width - cwidth(s))


def render_table(headers: list[str], rows: list[list[str]]) -> str:
    """渲染 markdown 管道表, 按 cwidth 算列宽 (中英文/emoji 等宽对齐)"""
    if not headers or not rows:
        return ""
    cols = len(headers)
    rows = [r + [""] * (cols - len(r)) for r in rows]
    widths = [cwidth(headers[i]) for i in range(cols)]
    for r in rows:
        for i in range(cols):
            widths[i] = max(widths[i], cwidth(str(r[i])))

    def fmt_row(cells: list[str]) -> str:
        return "| " + " | ".join(cpad(str(c), widths[i]) for i, c in enumerate(cells)) + " |"

    sep = "|" + "|".join("-" * (widths[i] + 2) for i in range(cols)) + "|"
    out = [fmt_row(headers), sep]
    out.extend(fmt_row(r) for r in rows)
    return "\n".join(out)


# ────────── offsets 持久化 (debounce 写盘) ──────────
def load_offsets(offsets_file: Path) -> dict[str, int]:
    if offsets_file.exists():
        try:
            return json.loads(offsets_file.read_text())
        except Exception as e:
            log.warning(f"load_offsets err (ignored): {e}")
    return {}


OFFSETS_SAVE_INTERVAL = 5.0
_last_offsets_save: float = 0.0


def save_offsets(offsets_file: Path, offsets: dict[str, int], *, force: bool = False) -> None:
    """debounce 写盘: 默认每 5s 至多 1 次。退出/session 切换时 force=True 立即写。"""
    global _last_offsets_save
    now = time.time()
    if not force and (now - _last_offsets_save) < OFFSETS_SAVE_INTERVAL:
        return
    _last_offsets_save = now
    offsets_file.parent.mkdir(exist_ok=True)
    tmp = offsets_file.with_suffix(".tmp")
    tmp.write_text(json.dumps(offsets, indent=2))
    tmp.replace(offsets_file)
