"""Pure Feishu Card JSON 2.0 rendering helpers."""
from __future__ import annotations

import json
from dataclasses import replace
from html.parser import HTMLParser
from typing import Any

from tmuxbot.core.rich_messages import ReplyBlock, ReplyDocument, reply_summary


class FeishuCardTooLarge(ValueError):
    """Raised when a serialized card exceeds Feishu's request-size limit."""


_STATE_TEMPLATES = {
    "working": "yellow",
    "idle": "green",
    "completed": "green",
    "waiting": "orange",
    "blocked": "red",
    "dead": "red",
    "error": "red",
    "info": "blue",
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
                "tag": "div",
                "element_id": "reply_status",
                "text": {
                    "tag": "plain_text",
                    "content": document.footer_text,
                    "text_size": "notation",
                    "text_color": "grey",
                },
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
        "title": {
            "tag": "plain_text",
            "content": f"{document.title} · {document.project_name}",
        },
        "subtitle": {"tag": "plain_text", "content": document.binding_name},
        "template": _STATE_TEMPLATES.get(
            "working" if streaming else document.state or "",
            "grey",
        ),
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


def serialize_feishu_reply_chunks(
    document: ReplyDocument,
    token: str,
    *,
    max_bytes: int = 30_000,
) -> list[str]:
    """Serialize a complete reply as as many valid Card JSON messages as needed."""
    if not document.blocks:
        return [serialize_feishu_card(build_feishu_card_v2(document, token), max_bytes=max_bytes)]

    groups: list[list[ReplyBlock]] = []
    current: list[ReplyBlock] = []
    pending = list(document.blocks)
    pack_limit = max(1, max_bytes - 512)
    while pending:
        block = pending.pop(0)
        candidate = current + [block]
        if _feishu_blocks_fit(document, candidate, token, pack_limit):
            current = candidate
            continue
        if current:
            groups.append(current)
            current = []
            pending.insert(0, block)
            continue
        split = _split_reply_block(block)
        if len(split) == 1:
            raise FeishuCardTooLarge("single Feishu reply block cannot be split further")
        pending = split + pending
    if current:
        groups.append(current)

    total = len(groups)
    payloads: list[str] = []
    for index, blocks in enumerate(groups, start=1):
        title = document.title if total == 1 else f"{document.title}（{index}/{total}）"
        chunk = replace(
            document,
            title=title,
            blocks=tuple(blocks),
            source_text=_reply_blocks_text(blocks),
            footer_text=document.footer_text if index == total else None,
        )
        try:
            payloads.append(
                serialize_feishu_card(
                    build_feishu_card_v2(chunk, token),
                    max_bytes=max_bytes,
                )
            )
        except FeishuCardTooLarge:
            if index != total or document.footer_text is None:
                raise
            without_footer = replace(chunk, footer_text=None)
            payloads.append(
                serialize_feishu_card(
                    build_feishu_card_v2(without_footer, token),
                    max_bytes=max_bytes,
                )
            )
            footer_chunk = replace(
                document,
                title=f"{document.title}（状态）",
                blocks=(),
                source_text="",
            )
            payloads.append(
                serialize_feishu_card(
                    build_feishu_card_v2(footer_chunk, token),
                    max_bytes=max_bytes,
                )
            )
    return payloads


def _feishu_blocks_fit(
    document: ReplyDocument,
    blocks: list[ReplyBlock],
    token: str,
    max_bytes: int,
) -> bool:
    candidate = replace(
        document,
        blocks=tuple(blocks),
        source_text=_reply_blocks_text(blocks),
        footer_text=None,
    )
    try:
        serialize_feishu_card(
            build_feishu_card_v2(candidate, token),
            max_bytes=max_bytes,
        )
    except FeishuCardTooLarge:
        return False
    return True


def _split_reply_block(block: ReplyBlock) -> list[ReplyBlock]:
    if block.kind == "list":
        if len(block.items) > 1:
            middle = len(block.items) // 2
            return [
                replace(block, items=block.items[:middle]),
                replace(block, items=block.items[middle:]),
            ]
        if block.items:
            left, right = _split_text_half(block.items[0])
            if right:
                return [replace(block, items=(left,)), replace(block, items=(right,))]
        return [block]
    left, right = _split_text_half(block.text)
    if not right:
        return [block]
    return [replace(block, text=left), replace(block, text=right)]


def _split_text_half(value: str) -> tuple[str, str]:
    if len(value) < 2:
        return value, ""
    middle = len(value) // 2
    candidates = [value.rfind("\n", 0, middle + 1), value.rfind(" ", 0, middle + 1)]
    split_at = max(candidates)
    if split_at <= 0:
        split_at = middle
    else:
        split_at += 1
    return value[:split_at], value[split_at:]


def _reply_blocks_text(blocks: list[ReplyBlock]) -> str:
    parts: list[str] = []
    for block in blocks:
        if block.kind == "list":
            parts.append("\n".join(f"- {item}" for item in block.items))
        else:
            parts.append(block.text)
    return "\n\n".join(part for part in parts if part)


def build_feishu_control_panel(markdown_text: str, token: str) -> dict[str, Any]:
    specs = [
        ("无需 @", "mention_on", "primary"),
        ("必须 @", "mention_off", "default"),
        ("继承默认", "mention_default", "default"),
        ("状态", "cmd_status", "default"),
        ("屏幕", "cmd_screen", "default"),
        ("新会话", "cmd_new", "danger"),
        ("压缩上下文", "cmd_compact", "default"),
        ("恢复会话", "cmd_resume", "default"),
        ("切换模型", "cmd_model", "primary"),
        ("Esc", "cmd_esc", "default"),
        ("Ctrl-C", "cmd_cc", "danger"),
        ("重启 CLI", "cmd_restart", "danger"),
        ("刷新", "refresh_panel", "default"),
        ("关闭", "close_panel", "default"),
    ]
    elements: list[dict[str, Any]] = [
        {"tag": "markdown", "element_id": "panel_body", "content": markdown_text}
    ]
    for index, (label, action, button_type) in enumerate(specs):
        button = _button_element(index, label, action, button_type, token)
        button["element_id"] = f"panel_action_{index}"
        if action == "cmd_new":
            button["confirm"] = {
                "title": {"tag": "plain_text", "content": "确认创建新会话？"},
                "text": {
                    "tag": "plain_text",
                    "content": "这会在当前 tmux CLI 中执行 /new。",
                },
            }
        if action == "cmd_restart":
            button["confirm"] = {
                "title": {"tag": "plain_text", "content": "确认重启 CLI？"},
                "text": {
                    "tag": "plain_text",
                    "content": "当前 tmux pane 会退出并重新启动 provider CLI。",
                },
            }
        elements.append(button)
    return {
        "schema": "2.0",
        "config": {
            "summary": {"content": "tmuxbot 控制面板"},
            "update_multi": True,
            "width_mode": "fill",
            "enable_forward": False,
        },
        "header": {
            "title": {"tag": "plain_text", "content": "tmuxbot 控制面板"},
            "subtitle": {"tag": "plain_text", "content": "中文 · tmux 原生 CLI"},
            "template": "blue",
        },
        "body": {"direction": "vertical", "vertical_spacing": "8px", "elements": elements},
    }


def build_feishu_interaction_card(
    markdown_text: str,
    token: str,
    *,
    session_model: bool = False,
) -> dict[str, Any]:
    specs = [
        ("↑", "up"),
        ("←", "left"),
        ("确认", "enter"),
        ("→", "right"),
        ("↓", "down"),
        ("取消", "esc"),
        ("刷新", "refresh"),
    ]
    if session_model:
        specs.insert(5, ("仅本会话", "model_session"))
    elements: list[dict[str, Any]] = [
        {"tag": "markdown", "element_id": "tui_body", "content": markdown_text}
    ]
    for index, (label, action) in enumerate(specs):
        button = _button_element(index, label, action, "primary" if action == "enter" else "default", token)
        button["element_id"] = f"tui_action_{index}"
        elements.append(button)
    return {
        "schema": "2.0",
        "config": {
            "summary": {"content": "TUI 交互控制"},
            "update_multi": True,
            "width_mode": "fill",
            "enable_forward": False,
        },
        "header": {
            "title": {"tag": "plain_text", "content": "TUI 交互控制"},
            "subtitle": {"tag": "plain_text", "content": "操作当前 tmux CLI"},
            "template": "yellow",
        },
        "body": {"direction": "vertical", "vertical_spacing": "8px", "elements": elements},
    }


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
