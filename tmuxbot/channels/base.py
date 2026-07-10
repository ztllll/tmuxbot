"""Channel normalization contract."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from tmuxbot.core.messages import AttachmentRef, IncomingMessage


class ChannelAdapter(ABC):
    @abstractmethod
    def normalize_incoming(
        self,
        message: Any,
        *,
        sender_id: int | str | None = None,
        attachments: tuple[AttachmentRef, ...] = (),
    ) -> IncomingMessage:
        """Convert a platform SDK message to the stable inbound contract."""
