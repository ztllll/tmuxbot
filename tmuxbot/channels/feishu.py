"""Feishu SDK message normalization."""

from __future__ import annotations

import json
import re
from typing import Any

from tmuxbot.channels.base import ChannelAdapter
from tmuxbot.core.messages import AttachmentRef, IncomingMessage


def feishu_mentions_bot(message: Any, bot_open_id: str | None) -> bool:
    if not bot_open_id:
        return False
    mentions = getattr(message, "mentions", None) or []
    return any(
        getattr(getattr(mention, "id", None), "open_id", None) == bot_open_id
        for mention in mentions
    )


def feishu_replies_to_bot(message: Any, outbound_message_ids: set[str]) -> bool:
    candidate_ids = (
        getattr(message, "parent_id", None),
        getattr(message, "root_id", None),
        getattr(message, "reply_to_message_id", None),
    )
    return any(mid in outbound_message_ids for mid in candidate_ids if mid)


class FeishuChannelAdapter(ChannelAdapter):
    def __init__(
        self,
        *,
        bot_open_id: str | None = None,
        outbound_message_ids: set[str] | None = None,
        chat_type: str | None = None,
    ) -> None:
        self.bot_open_id = bot_open_id
        self.outbound_message_ids = outbound_message_ids if outbound_message_ids is not None else set()
        self.chat_type = chat_type

    def normalize_incoming(
        self,
        message: Any,
        *,
        sender_id: int | str | None = None,
        attachments: tuple[AttachmentRef, ...] = (),
    ) -> IncomingMessage:
        chat_type = str(self.chat_type or getattr(message, "chat_type", "") or "")
        text = _content_text(message)
        return IncomingMessage(
            source_id=getattr(message, "chat_id", ""),
            sender_id=sender_id or "",
            text=text,
            thread_id=None,
            platform_message_id=getattr(message, "message_id", None),
            direct_chat=chat_type == "p2p",
            mentioned=feishu_mentions_bot(message, self.bot_open_id),
            replied_to_bot=feishu_replies_to_bot(message, self.outbound_message_ids),
            command=_command_from_text(text),
            attachments=attachments,
            metadata={
                "channel": "feishu",
                "chat_type": chat_type,
                "message_type": getattr(message, "message_type", None),
            },
        )


def _content_text(message: Any) -> str:
    raw = getattr(message, "content", "") or ""
    try:
        content = json.loads(raw) if isinstance(raw, str) else (raw or {})
    except (json.JSONDecodeError, TypeError):
        return str(raw).strip()
    if not isinstance(content, dict):
        return str(raw).strip()
    message_type = getattr(message, "message_type", "")
    if message_type == "post":
        parts = [str(content.get("title") or "")]
        for line in content.get("content", []) or []:
            for node in line or []:
                if node.get("tag") in {"text", "a"}:
                    value = node.get("text") or node.get("href") or ""
                    if value:
                        parts.append(str(value))
        text = "\n".join(part for part in parts if part)
    else:
        text = str(
            content.get("text")
            or content.get("file_name")
            or content.get("fileName")
            or content.get("name")
            or ""
        )
    return re.sub(r"@_user_\d+\s*", "", text).strip()


def _command_from_text(text: str) -> str | None:
    if not text.startswith("/"):
        return None
    return text.split(maxsplit=1)[0]
