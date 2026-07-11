"""Pure Feishu Card JSON 2.0 rendering helpers."""
from __future__ import annotations

import json
from html.parser import HTMLParser
from typing import Any

from tmuxbot.core.rich_messages import ReplyBlock, ReplyDocument, reply_summary


class FeishuCardTooLarge(ValueError):
    """Raised when a serialized card exceeds Feishu's request-size limit."""


_STATE_TEMPLATES = {
    "working": "blue",
    "idle": "green",
    "waiting": "orange",
    "blocked": "red",
    "dead": "red",
}

def build_feishu_card_v2(
    document: ReplyDocument,
    token: str,
    *,
    confirm_interrupt: bool = False,
    streaming: bool = False,
) -> dict[str, Any]:
    elements = [_block_element(block, index) for index, block in enumerate(document.blocks)]
    if document.footer_text:
        elements.append(
            {
                "tag": "note",
                "element_id": "reply_status",
                "elements": [
                    {
                        "tag": "plain_text",
                        "content": document.footer_text,
                    }
                ],
            }
        )

    action_specs = (
        (("确认中断", "ctrl_c", "danger"), ("取消", "refresh", "default"))
        if confirm_interrupt
        else ()
    )
    for index, (label, action, button_type) in enumerate(action_specs):
        elements.append(_button_element(index, label, action, button_type, token))

    header: dict[str, Any] = {
        "title": {"tag": "plain_text", "content": document.title},
        "subtitle": {"tag": "plain_text", "content": document.binding_name},
        "template": _STATE_TEMPLATES.get(document.state or "", "grey"),
        "padding": "12px 12px 12px 12px",
    }
    if document.provider:
        header["text_tag_list"] = [
            {
                "tag": "text_tag",
                "element_id": "provider_tag",
                "text": {"tag": "plain_text", "content": document.provider},
                "color": "neutral",
            }
        ]

    summary = reply_summary(document) or document.title
    return {
        "schema": "2.0",
        "config": {
            "streaming_mode": streaming,
            "summary": {"content": summary},
            "enable_forward": True,
            "update_multi": True,
            "width_mode": "fill",
        },
        "header": header,
        "body": {
            "direction": "vertical",
            "padding": "12px 12px 12px 12px",
            "vertical_spacing": "8px",
            "elements": elements or [
                {
                    "tag": "markdown",
                    "element_id": "reply_empty",
                    "content": "（空）",
                }
            ],
        },
    }


def serialize_feishu_card(card: dict[str, Any], *, max_bytes: int = 30_000) -> str:
    serialized = json.dumps(card, ensure_ascii=False, separators=(",", ":"))
    size = len(serialized.encode("utf-8"))
    if size > max_bytes:
        raise FeishuCardTooLarge(f"Feishu card is {size} bytes; limit is {max_bytes}")
    return serialized


def _block_element(block: ReplyBlock, index: int) -> dict[str, Any]:
    return {
        "tag": "markdown",
        "element_id": f"reply_body_{index}",
        "content": _block_markdown(block),
    }


def _block_markdown(block: ReplyBlock) -> str:
    if block.kind == "heading":
        level = min(max(block.level, 1), 6)
        return f"{'#' * level} {html_to_feishu_markdown(block.text)}"
    if block.kind == "code":
        language = block.language or ""
        return f"```{language}\n{block.text}\n```"
    if block.kind == "quote":
        return "\n".join(f"> {line}" for line in block.text.splitlines())
    if block.kind == "list":
        return "\n".join(f"- {html_to_feishu_markdown(item)}" for item in block.items)
    if block.kind == "divider":
        return "---"
    return html_to_feishu_markdown(block.text)


def _button_element(
    index: int,
    label: str,
    action: str,
    button_type: str,
    token: str,
) -> dict[str, Any]:
    return {
        "tag": "button",
        "element_id": f"reply_action_{index}",
        "type": button_type,
        "size": "small",
        "width": "default",
        "text": {"tag": "plain_text", "content": label},
        "behaviors": [
            {
                "type": "callback",
                "value": {"token": token, "action": action},
            }
        ],
    }


def html_to_feishu_markdown(value: str) -> str:
    parser = _InlineMarkdownParser()
    parser.feed(value)
    parser.close()
    return "".join(parser.parts)


class _InlineMarkdownParser(HTMLParser):
    _OPEN = {
        "b": "**",
        "strong": "**",
        "i": "*",
        "em": "*",
        "s": "~~",
        "del": "~~",
        "code": "`",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in self._OPEN:
            self.parts.append(self._OPEN[tag])
        elif tag == "br":
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._OPEN:
            self.parts.append(self._OPEN[tag])

    def handle_data(self, data: str) -> None:
        self.parts.append(data)
