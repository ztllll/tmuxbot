"""Channel-neutral outbound reply contract."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping

from tmuxbot.core.events import TerminalStatus


@dataclass(frozen=True, slots=True)
class ReplyEnvelope:
    title: str
    body: str
    footer: TerminalStatus | None = None
    attachments: tuple[str, ...] = ()
    actions: tuple[str, ...] = ()
    replace_key: str | None = None
    notify: bool = True
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "attachments", tuple(self.attachments))
        object.__setattr__(self, "actions", tuple(self.actions))
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))
