"""Assistant reply rendering shared by IM frontends."""
from __future__ import annotations

import html
import re

from tmuxbot.core.replies import ReplyEnvelope
from tmuxbot.core.rich_messages import (
    RenderedReply,
    build_reply_document,
    render_telegram_document,
)
from tmuxbot.state import Binding
from tmuxbot.utils import strip_decorations, utf16_len


AssistantReply = RenderedReply


def render_assistant_reply(
    b: Binding,
    envelope: ReplyEnvelope,
    *,
    full_output_threshold: int | None,
    footer_text: str | None = None,
) -> AssistantReply:
    """Build the readable in-chat assistant reply.

    ``envelope.body`` is already escaped by backend parsers, so this function only
    wraps it with metadata and optionally creates a plain full-output payload.
    """
    document = build_reply_document(b, envelope, footer_text=footer_text)
    return render_telegram_document(
        document,
        full_output_threshold=full_output_threshold,
    )


def screen_footer_from_capture(raw: str) -> str | None:
    clean = strip_decorations(raw)
    for line in reversed(clean.splitlines()):
        line = line.strip()
        if line and not _is_uninformative_footer_line(line):
            return line[:240]
    return None


def _is_uninformative_footer_line(line: str) -> bool:
    compact = line.strip()
    if not compact:
        return True
    if re.fullmatch(r"[›>❯»]\s*", compact):
        return True
    if re.fullmatch(r"[│┃┆┊╎╏╭╮╰╯─━═╱╲╳+|\-\s]+", compact):
        return True
    return False


def html_to_plain_text(html_text: str) -> str:
    no_tags = re.sub(r"<[^>]+>", "", html_text)
    return html.unescape(no_tags)


def format_markdownish_html(html_text: str) -> str:
    parts = re.split(r"(```[A-Za-z0-9_+-]*\n.*?```)", html_text, flags=re.S)
    formatted: list[str] = []
    for part in parts:
        if part.startswith("```"):
            formatted.append(_format_code_fence(part))
        else:
            formatted.append(_format_headings(part))
    return "".join(formatted)


def _format_code_fence(text: str) -> str:
    match = re.fullmatch(r"```([A-Za-z0-9_+-]*)\n(.*?)```", text, flags=re.S)
    if not match:
        return text
    lang, code = match.groups()
    code = code.rstrip("\n")
    if lang:
        return f'<pre><code class="language-{html.escape(lang)}">{code}</code></pre>'
    return f"<pre>{code}</pre>"


def _format_headings(text: str) -> str:
    return re.sub(
        r"(?m)^(#{1,3})\s+(.+)$",
        lambda m: f"<b>{m.group(2).strip()}</b>",
        text,
    )


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
