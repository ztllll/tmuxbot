"""Shared "is this message addressed to the bot?" policy."""
from __future__ import annotations

from tmuxbot.core.messages import IncomingMessage


def message_is_addressed_to_bot(
    *,
    require_addressing: bool,
    direct_chat: bool,
    mentioned: bool,
    replied_to_bot: bool,
) -> bool:
    """Apply the common wakeup policy across IM frontends."""
    if not require_addressing:
        return True
    if direct_chat:
        return True
    return bool(mentioned or replied_to_bot)


def incoming_message_is_addressed(
    message: IncomingMessage, *, require_addressing: bool
) -> bool:
    return message_is_addressed_to_bot(
        require_addressing=require_addressing,
        direct_chat=message.direct_chat,
        mentioned=message.mentioned,
        replied_to_bot=message.replied_to_bot,
    )
