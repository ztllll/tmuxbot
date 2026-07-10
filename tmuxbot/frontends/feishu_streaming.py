"""State machine for Feishu CardKit streaming text updates."""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable


class StreamingPrefixError(ValueError):
    """Raised when a streaming update does not extend the previous text."""


UpdateContent = Callable[[str, str, str, int], Awaitable[bool]]
CloseCard = Callable[[str, dict[str, Any], int], Awaitable[bool]]


@dataclass(slots=True)
class FeishuStreamingSession:
    card_id: str
    element_id: str
    update_content: UpdateContent
    close_card: CloseCard
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    min_interval: float = 0.2
    text: str = ""
    sequence: int = 0
    closed: bool = False
    failed: bool = False
    _last_update: float | None = field(default=None, init=False, repr=False)

    async def append(self, text: str) -> bool:
        if self.closed or self.failed:
            return False
        if not text.startswith(self.text):
            raise StreamingPrefixError("streaming content must extend the previous text")
        if text == self.text:
            return True

        now = self.clock()
        if self._last_update is not None:
            delay = self.min_interval - (now - self._last_update)
            if delay > 0:
                await self.sleep(delay)
        self.sequence += 1
        ok = await self.update_content(
            self.card_id,
            self.element_id,
            text,
            self.sequence,
        )
        if not ok:
            self.failed = True
            return False
        self.text = text
        self._last_update = self.clock()
        return True

    async def close(self, final_card: dict[str, Any]) -> bool:
        if self.closed or self.failed:
            return False
        self.sequence += 1
        ok = await self.close_card(self.card_id, final_card, self.sequence)
        if not ok:
            self.failed = True
            return False
        self.closed = True
        return True
