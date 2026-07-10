"""Runtime V2 cutover router with content-safe shadow diagnostics."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from enum import Enum
from typing import Callable

from tmuxbot.core.event_reducer import ReducedEvent, reduce_provider_event
from tmuxbot.core.events import ProviderEvent

log = logging.getLogger("tmuxbot")
Reducer = Callable[[ProviderEvent], list[ReducedEvent]]


class RuntimeMode(str, Enum):
    OFF = "off"
    SHADOW = "shadow"
    ON = "on"


@dataclass(frozen=True, slots=True)
class RuntimeDecision:
    deliveries: tuple[ReducedEvent, ...]
    shadow: tuple[ReducedEvent, ...] = ()
    parity: bool = True


def reduce_v2_event(event: ProviderEvent) -> list[ReducedEvent]:
    """V2 delivery reduction; separate entry point enables safe shadow evolution."""
    return reduce_provider_event(event)


class RuntimeV2Router:
    def __init__(
        self,
        mode: RuntimeMode,
        *,
        legacy_reducer: Reducer = reduce_provider_event,
        v2_reducer: Reducer = reduce_v2_event,
        logger: logging.Logger = log,
    ) -> None:
        self.mode = mode
        self.legacy_reducer = legacy_reducer
        self.v2_reducer = v2_reducer
        self.logger = logger

    @classmethod
    def from_environment(cls) -> "RuntimeV2Router":
        raw = os.getenv("TMUXBOT_RUNTIME_V2", "off").strip().lower()
        try:
            mode = RuntimeMode(raw)
        except ValueError:
            mode = RuntimeMode.OFF
        return cls(mode)

    def route(self, event: ProviderEvent) -> RuntimeDecision:
        if self.mode == RuntimeMode.OFF:
            return RuntimeDecision(deliveries=tuple(self.legacy_reducer(event)))
        if self.mode == RuntimeMode.ON:
            return RuntimeDecision(deliveries=tuple(self.v2_reducer(event)))

        legacy = tuple(self.legacy_reducer(event))
        shadow = tuple(self.v2_reducer(event))
        parity = legacy == shadow
        if not parity:
            self.logger.warning(
                "runtime v2 shadow mismatch event_kind=%s legacy=%s v2=%s",
                event.kind.value,
                _redacted_shape(legacy),
                _redacted_shape(shadow),
            )
        return RuntimeDecision(deliveries=legacy, shadow=shadow, parity=parity)


def _redacted_shape(events: tuple[ReducedEvent, ...]) -> tuple[tuple[str, bool, str], ...]:
    return tuple(
        (event.kind, bool(event.body), _length_bucket(len(event.body)))
        for event in events
    )


def _length_bucket(length: int) -> str:
    if length == 0:
        return "0"
    if length <= 32:
        return "1-32"
    if length <= 256:
        return "33-256"
    if length <= 2048:
        return "257-2048"
    return "2049+"
