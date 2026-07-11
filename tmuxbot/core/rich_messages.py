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
    items: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ReplyDocument:
    title: str
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


def telegram_state_badge(state: str | None) -> str | None:
    if state is None:
        return None
    return _TELEGRAM_STATE_BADGES.get(state, "⚪ <b>状态未知</b>")


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
    return ReplyDocument(
        title=envelope.title or "回复",
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

    header = (
        f"💬 <b>{html.escape(rendered_document.title)}</b> · "
        f"<code>{html.escape(rendered_document.binding_name)}</code>"
    )
    body = "\n\n".join(_render_telegram_block(block) for block in rendered_document.blocks)
    parts = [header]
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

        fence = re.match(r"^```([A-Za-z0-9_+-]*)\s*$", stripped)
        if fence:
            flush_paragraph()
            language = fence.group(1) or None
            index += 1
            code_lines: list[str] = []
            while index < len(lines) and lines[index].strip() != "```":
                code_lines.append(lines[index])
                index += 1
            if index < len(lines):
                index += 1
            blocks.append(ReplyBlock("code", "\n".join(code_lines), language=language))
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
        if block.language:
            return (
                f'<pre><code class="language-{html.escape(block.language)}">'
                f"{code}</code></pre>"
            )
        return f"<pre>{code}</pre>"
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

    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        attr_map = dict(attrs)
        if tag in self._SIMPLE_TAGS:
            self.parts.append(f"<{tag}>")
            return
        if tag == "code":
            language = attr_map.get("class", "")
            if re.fullmatch(r"language-[A-Za-z0-9_+-]+", language):
                self.parts.append(f'<code class="{html.escape(language, quote=True)}">')
            else:
                self.parts.append("<code>")
            return
        if tag == "blockquote":
            expandable = any(name == "expandable" for name, _value in attrs)
            self.parts.append("<blockquote expandable>" if expandable else "<blockquote>")
            return
        if tag == "span" and attr_map.get("class") == "tg-spoiler":
            self.parts.append('<span class="tg-spoiler">')
            return
        if tag == "tg-spoiler":
            self.parts.append("<tg-spoiler>")
            return
        if tag == "a" and attr_map.get("href"):
            href = html.escape(attr_map["href"], quote=True)
            self.parts.append(f'<a href="{href}">')
            return
        self.parts.append(html.escape(self.get_starttag_text() or f"<{tag}>", quote=False))

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        allowed = self._SIMPLE_TAGS | {"code", "blockquote", "tg-spoiler", "a"}
        if tag in allowed:
            self.parts.append(f"</{tag}>")
        elif tag == "span":
            self.parts.append("</span>")
        else:
            self.parts.append(html.escape(f"</{tag}>", quote=False))

    def handle_data(self, data: str) -> None:
        self.parts.append(html.escape(data, quote=False))

    def handle_entityref(self, name: str) -> None:
        value = html.unescape(f"&{name};")
        self.parts.append(html.escape(value, quote=False))

    def handle_charref(self, name: str) -> None:
        value = html.unescape(f"&#{name};")
        self.parts.append(html.escape(value, quote=False))


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
