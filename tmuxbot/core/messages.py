"""Normalized inbound messages from communication channels."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping


@dataclass(frozen=True, slots=True)
class AttachmentRef:
    path: str
    kind: str = "file"
    name: str | None = None
    mime_type: str | None = None


@dataclass(frozen=True, slots=True)
class IncomingMessage:
    source_id: int | str
    sender_id: int | str
    text: str = ""
    thread_id: int | str | None = None
    platform_message_id: int | str | None = None
    direct_chat: bool = False
    mentioned: bool = False
    replied_to_bot: bool = False
    command: str | None = None
    attachments: tuple[AttachmentRef, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "attachments", tuple(self.attachments))
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))
