"""Shared "is this message addressed to the bot?" policy."""
from __future__ import annotations


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
