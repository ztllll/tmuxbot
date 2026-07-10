"""Telegram SDK message normalization."""

from __future__ import annotations

import re
from typing import Any

from tmuxbot.channels.base import ChannelAdapter
from tmuxbot.core.messages import AttachmentRef, IncomingMessage


def telegram_mentions_bot(message: Any, bot_username: str | None) -> bool:
    if not bot_username:
        return False
    username = bot_username.lstrip("@")
    if not username:
        return False
    pattern = re.compile(rf"@{re.escape(username)}(?![A-Za-z0-9_])", re.IGNORECASE)
    return any(
        pattern.search(value or "")
        for value in (getattr(message, "text", None), getattr(message, "caption", None))
    )


def telegram_replies_to_bot(
    message: Any, bot_username: str | None, bot_id: int | None
) -> bool:
    reply = getattr(message, "reply_to_message", None)
    user = getattr(reply, "from_user", None)
    if user is None:
        return False
    if bot_id is not None and getattr(user, "id", None) == bot_id:
        return True
    if bot_username:
        username = str(getattr(user, "username", "") or "").lstrip("@").lower()
        return username == bot_username.lstrip("@").lower()
    return False


class TelegramChannelAdapter(ChannelAdapter):
    def __init__(self, *, bot_username: str | None = None, bot_id: int | None = None):
        self.bot_username = bot_username
        self.bot_id = bot_id

    def normalize_incoming(
        self,
        message: Any,
        *,
        sender_id: int | str | None = None,
        attachments: tuple[AttachmentRef, ...] = (),
    ) -> IncomingMessage:
        chat = getattr(message, "chat", None)
        chat_type = getattr(chat, "type", "")
        direct_chat = chat_type == "private"
        is_topic = bool(getattr(message, "is_topic_message", False)) and not direct_chat
        thread_id = getattr(message, "message_thread_id", None) if is_topic else None
        text = str(
            getattr(message, "text", None)
            or getattr(message, "caption", None)
            or ""
        ).strip()
        actual_sender = sender_id
        if actual_sender is None:
            actual_sender = getattr(getattr(message, "from_user", None), "id", "")
        mentioned = telegram_mentions_bot(message, self.bot_username)
        replied = telegram_replies_to_bot(message, self.bot_username, self.bot_id)
        return IncomingMessage(
            source_id=getattr(chat, "id", ""),
            sender_id=actual_sender,
            text=text,
            thread_id=thread_id,
            platform_message_id=getattr(message, "message_id", None),
            direct_chat=direct_chat,
            mentioned=mentioned,
            replied_to_bot=replied,
            command=_command_from_text(text),
            attachments=attachments,
            metadata={"channel": "telegram", "chat_type": chat_type},
        )


def _command_from_text(text: str) -> str | None:
    if not text.startswith("/"):
        return None
    token = text.split(maxsplit=1)[0]
    return token.split("@", 1)[0]
