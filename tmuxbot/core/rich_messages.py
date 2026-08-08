"""Channel-neutral assistant reply documents and platform renderers."""
from __future__ import annotations

import html
import re
from dataclasses import dataclass, field, replace
from html.parser import HTMLParser
from types import MappingProxyType
from typing import Any, Mapping

from tmuxbot.core.replies import ReplyEnvelope
from tmuxbot.state import Binding
from tmuxbot.utils import utf16_len


@dataclass(frozen=True, slots=True)
class ReplyBlock:
    kind: str
    text: str = ""
    level: int = 0
    language: str | None = None
    filename: str | None = None
    items: tuple[str, ...] = ()
    headers: tuple[str, ...] = ()
    rows: tuple[tuple[str, ...], ...] = ()


@dataclass(frozen=True, slots=True)
class ReplyDocument:
    title: str
    project_name: str
    binding_name: str
    blocks: tuple[ReplyBlock, ...]
    source_text: str
    footer_text: str | None = None
    provider: str | None = None
    state: str | None = None
    actions: tuple[str, ...] = ()
    attachments: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "blocks", tuple(self.blocks))
        object.__setattr__(self, "actions", tuple(self.actions))
        object.__setattr__(self, "attachments", tuple(self.attachments))
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))


@dataclass(frozen=True, slots=True)
class RenderedReply:
    chat_html: str
    full_text: str | None = None


_TELEGRAM_STATE_BADGES = {
    "working": "🟡 <b>工作中</b>",
    "waiting": "🟠 <b>等待输入</b>",
    "completed": "✅ <b>已完成</b>",
    "idle": "✅ <b>已完成</b>",
    "error": "🔴 <b>错误/阻塞</b>",
    "blocked": "🔴 <b>错误/阻塞</b>",
    "dead": "🔴 <b>错误/阻塞</b>",
    "info": "🔵 <b>信息</b>",
}

_PLAIN_STATE_BADGES = {
    "working": "🟡 工作中",
    "waiting": "🟠 等待输入",
    "completed": "✅ 已完成",
    "idle": "✅ 已完成",
    "error": "🔴 错误/阻塞",
    "blocked": "🔴 错误/阻塞",
    "dead": "🔴 错误/阻塞",
    "info": "🔵 信息",
}

_STATE_TITLES = {
    "working": "工作中",
    "waiting": "等待输入",
    "completed": "已完成",
    "idle": "已完成",
    "error": "错误/阻塞",
    "blocked": "错误/阻塞",
    "dead": "错误/阻塞",
    "info": "信息",
}


def telegram_state_badge(state: str | None) -> str | None:
    if state is None:
        return None
    return _TELEGRAM_STATE_BADGES.get(state, "⚪ <b>状态未知</b>")


def plain_state_badge(state: str | None) -> str | None:
    if state is None:
        return None
    return _PLAIN_STATE_BADGES.get(state, "⚪ 状态未知")


def build_reply_document(
    binding: Binding,
    envelope: ReplyEnvelope,
    footer_text: str | None = None,
) -> ReplyDocument:
    source = envelope.body
    provider = envelope.metadata.get("provider") or binding.backend
    display_state = envelope.metadata.get("display_state")
    state = (
        str(display_state)
        if display_state
        else envelope.footer.state.value if envelope.footer is not None else None
    )
    title = envelope.title or "回复"
    if title == "tmuxbot":
        title = _STATE_TITLES.get(state or "", "状态更新")
    return ReplyDocument(
        title=title,
        project_name=binding.cwd.name or binding.name,
        binding_name=binding.name,
        blocks=_parse_blocks(source),
        source_text=source,
        footer_text=footer_text,
        provider=str(provider) if provider else None,
        state=state,
        actions=envelope.actions,
        attachments=envelope.attachments,
        metadata=envelope.metadata,
    )


def render_telegram_document(
    document: ReplyDocument,
    *,
    full_output_threshold: int | None,
) -> RenderedReply:
    rendered_document = document
    full_text = None
    if (
        full_output_threshold is not None
        and utf16_len(document.source_text) > full_output_threshold
    ):
        full_text = _plain_text(document.source_text)
        preview = _truncate_by_lines(document.source_text, full_output_threshold // 2)
        preview = f"{preview}\n\n<i>完整输出已附为文件。</i>"
        rendered_document = replace(document, blocks=_parse_blocks(preview), source_text=preview)

    header = f"💬 <b>{html.escape(rendered_document.title)} · {html.escape(rendered_document.project_name)}</b>"
    session = f"<i>会话 · <code>{html.escape(rendered_document.binding_name)}</code></i>"
    body = "\n\n".join(_render_telegram_block(block) for block in rendered_document.blocks)
    parts = [header, session]
    state_badge = telegram_state_badge(rendered_document.state)
    if state_badge:
        parts.append(state_badge)
    if body:
        parts.append(body)
    if rendered_document.footer_text:
        parts.append(f"<i>{html.escape(rendered_document.footer_text)}</i>")
    return RenderedReply(chat_html="\n\n".join(parts), full_text=full_text)


def reply_summary(document: ReplyDocument, limit: int = 120) -> str:
    text = _plain_text(document.source_text)
    text = re.sub(r"(?m)^#{1,6}\s+", "", text)
    text = re.sub(r"(?m)^[-*+]\s+", "", text)
    text = re.sub(r"(?m)^>\s?", "", text)
    text = re.sub(r"```[A-Za-z0-9_+-]*", "", text).replace("```", "")
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 1)].rstrip() + "…"


def _parse_blocks(source: str) -> tuple[ReplyBlock, ...]:
    lines = source.splitlines()
    blocks: list[ReplyBlock] = []
    paragraph: list[str] = []
    index = 0

    def flush_paragraph() -> None:
        if paragraph:
            blocks.append(ReplyBlock("paragraph", "\n".join(paragraph).strip()))
            paragraph.clear()

    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        if not stripped:
            flush_paragraph()
            index += 1
            continue

        fence = re.match(r"^```([^`]*)$", stripped)
        if fence:
            flush_paragraph()
            language, filename = _parse_fence_info(fence.group(1))
            index += 1
            code_lines: list[str] = []
            while index < len(lines) and lines[index].strip() != "```":
                code_lines.append(lines[index])
                index += 1
            if index < len(lines):
                index += 1
            blocks.append(
                ReplyBlock(
                    "code",
                    "\n".join(code_lines),
                    language=language,
                    filename=filename,
                )
            )
            continue

        table = _parse_table(lines, index)
        if table is not None:
            flush_paragraph()
            block, index = table
            blocks.append(block)
            continue

        heading = re.match(r"^(#{1,6})\s+(.+)$", stripped)
        if heading:
            flush_paragraph()
            blocks.append(
                ReplyBlock("heading", heading.group(2).strip(), level=len(heading.group(1)))
            )
            index += 1
            continue

        if re.fullmatch(r"(?:-{3,}|\*{3,}|_{3,})", stripped):
            flush_paragraph()
            blocks.append(ReplyBlock("divider"))
            index += 1
            continue

        if re.match(r"^>\s?", stripped):
            flush_paragraph()
            quote_lines: list[str] = []
            while index < len(lines) and re.match(r"^\s*>\s?", lines[index]):
                quote_lines.append(re.sub(r"^\s*>\s?", "", lines[index]))
                index += 1
            blocks.append(ReplyBlock("quote", "\n".join(quote_lines).strip()))
            continue

        if re.match(r"^\s*(?:[-*+]\s+|\d+[.)]\s+)", line):
            flush_paragraph()
            items: list[str] = []
            while index < len(lines):
                item = re.match(r"^\s*(?:[-*+]\s+|\d+[.)]\s+)(.+)$", lines[index])
                if item is None:
                    break
                items.append(item.group(1).strip())
                index += 1
            blocks.append(ReplyBlock("list", items=tuple(items)))
            continue

        paragraph.append(line)
        index += 1

    flush_paragraph()
    return tuple(blocks)


def _render_telegram_block(block: ReplyBlock) -> str:
    if block.kind == "heading":
        return f"<b>{_sanitize_telegram_inline(block.text)}</b>"
    if block.kind == "code":
        code = html.escape(html.unescape(block.text), quote=False)
        label = (
            f"📄 <code>{html.escape(block.filename)}</code>\n"
            if block.filename
            else ""
        )
        if block.language:
            return (
                f'{label}<pre><code class="language-{html.escape(block.language)}">'
                f"{code}</code></pre>"
            )
        return f"{label}<pre>{code}</pre>"
    if block.kind == "table":
        return _render_telegram_table(block)
    if block.kind == "quote":
        return f"<blockquote expandable>{_sanitize_telegram_inline(block.text)}</blockquote>"
    if block.kind == "list":
        return "\n".join(f"• {_sanitize_telegram_inline(item)}" for item in block.items)
    if block.kind == "divider":
        return "────────"
    return _sanitize_telegram_inline(block.text)


_TELEGRAM_INLINE_TAG_RE = re.compile(
    r"(</?(?:b|strong|i|em|u|ins|s|strike|del|code)\s*>)",
    re.IGNORECASE,
)


def _sanitize_telegram_inline(value: str) -> str:
    return sanitize_telegram_html(value)


def sanitize_telegram_html(value: str) -> str:
    parser = _TelegramHTMLSanitizer()
    parser.feed(value)
    parser.close()
    return "".join(parser.parts)


class _TelegramHTMLSanitizer(HTMLParser):
    _SIMPLE_TAGS = {
        "b",
        "strong",
        "i",
        "em",
        "u",
        "ins",
        "s",
        "strike",
        "del",
        "pre",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.parts: list[str] = []
        self.stack: list[tuple[str, str]] = []
        self.autoclosed: list[str] = []

    def close(self) -> None:
        super().close()
        while self.stack:
            tag, _start = self.stack.pop()
            self.parts.append(f"</{tag}>")

    def _open(self, tag: str, start: str) -> None:
        self.parts.append(start)
        self.stack.append((tag, start))

    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        attr_map = dict(attrs)
        if tag in self._SIMPLE_TAGS:
            self._open(tag, f"<{tag}>")
            return
        if tag == "code":
            language = attr_map.get("class", "")
            if re.fullmatch(r"language-[A-Za-z0-9_+-]+", language):
                start = f'<code class="{html.escape(language, quote=True)}">'
            else:
                start = "<code>"
            self._open("code", start)
            return
        if tag == "blockquote":
            expandable = any(name == "expandable" for name, _value in attrs)
            start = "<blockquote expandable>" if expandable else "<blockquote>"
            self._open("blockquote", start)
            return
        if tag == "span" and attr_map.get("class") == "tg-spoiler":
            self._open("span", '<span class="tg-spoiler">')
            return
        if tag == "tg-spoiler":
            self._open("tg-spoiler", "<tg-spoiler>")
            return
        if tag == "a" and attr_map.get("href"):
            href = html.escape(attr_map["href"], quote=True)
            self._open("a", f'<a href="{href}">')
            return
        self.parts.append(html.escape(self.get_starttag_text() or f"<{tag}>", quote=False))

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag not in {open_tag for open_tag, _start in self.stack}:
            if tag in self.autoclosed:
                self.autoclosed.remove(tag)
                return
            self.parts.append(html.escape(f"</{tag}>", quote=False))
            return
        while self.stack:
            open_tag, _start = self.stack.pop()
            self.parts.append(f"</{open_tag}>")
            if open_tag == tag:
                break
            self.autoclosed.append(open_tag)

    def handle_data(self, data: str) -> None:
        self.parts.append(html.escape(data, quote=False))

    def handle_entityref(self, name: str) -> None:
        value = html.unescape(f"&{name};")
        self.parts.append(html.escape(value, quote=False))

    def handle_charref(self, name: str) -> None:
        value = html.unescape(f"&#{name};")
        self.parts.append(html.escape(value, quote=False))


def _parse_fence_info(value: str) -> tuple[str | None, str | None]:
    info = value.strip()
    if not info:
        return None, None
    tokens = info.split()
    language = tokens[0] if re.fullmatch(r"[A-Za-z0-9_+-]+", tokens[0]) else None
    filename = None
    for token in tokens[1:] if language else tokens:
        if token.startswith("filename="):
            filename = token.removeprefix("filename=").strip('"\'') or None
            break
        if token.startswith("file="):
            filename = token.removeprefix("file=").strip('"\'') or None
            break
    return language, filename


def _split_table_row(line: str) -> tuple[str, ...]:
    value = line.strip().strip("|")
    return tuple(cell.strip() for cell in re.split(r"(?<!\\)\|", value))


def _parse_table(lines: list[str], index: int) -> tuple[ReplyBlock, int] | None:
    if index + 1 >= len(lines) or "|" not in lines[index]:
        return None
    headers = _split_table_row(lines[index])
    separator = _split_table_row(lines[index + 1])
    if len(headers) < 2 or len(separator) != len(headers):
        return None
    if not all(re.fullmatch(r":?-{3,}:?", cell.replace(" ", "")) for cell in separator):
        return None
    rows: list[tuple[str, ...]] = []
    cursor = index + 2
    while cursor < len(lines) and "|" in lines[cursor] and lines[cursor].strip():
        row = _split_table_row(lines[cursor])
        if len(row) < len(headers):
            row += ("",) * (len(headers) - len(row))
        rows.append(row[: len(headers)])
        cursor += 1
    return ReplyBlock("table", headers=headers, rows=tuple(rows)), cursor


def _render_telegram_table(block: ReplyBlock) -> str:
    rows = [block.headers, *block.rows]
    widths = [
        max(len(_plain_text(row[index])) for row in rows)
        for index in range(len(block.headers))
    ]
    rendered: list[str] = []
    for row_index, row in enumerate(rows):
        rendered.append(
            "  ".join(
                _plain_text(cell).ljust(widths[index])
                for index, cell in enumerate(row)
            ).rstrip()
        )
        if row_index == 0:
            rendered.append("  ".join("─" * width for width in widths))
    return f"<pre>{html.escape(chr(10).join(rendered), quote=False)}</pre>"


def _plain_text(value: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", value))


def _truncate_by_lines(text: str, limit: int) -> str:
    lines: list[str] = []
    total = 0
    for line in text.splitlines():
        line_len = utf16_len(line) + 1
        if total + line_len > limit and lines:
            break
        lines.append(line)
        total += line_len
    return "\n".join(lines).rstrip() + "\n<i>… 已截断预览</i>"
