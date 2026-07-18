"""Channel-neutral connection health and recovery audit.

The bridge deliberately keeps message handling channel-specific, but the
operational contract is the same for every frontend: register, report a live
transport, report accepted input, and leave an inspectable record on disk.
The systemd refresh template consumes the same contract for Telegram and
Feishu instances; no channel gets a private "fake alive" workaround.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path

log = logging.getLogger("tmuxbot")


@dataclass(slots=True)
class ChannelHealth:
    id: str
    channel: str
    credential_scope: str
    binding_count: int
    state: str = "starting"
    started_at: float = 0.0
    connected_at: float | None = None
    last_transport_at: float | None = None
    last_inbound_at: float | None = None
    last_error: str | None = None
    recovery_count: int = 0


class ChannelHealthRegistry:
    """Small in-process registry with an atomic, read-only audit snapshot."""

    def __init__(self) -> None:
        self._channels: dict[str, ChannelHealth] = {}

    def register(
        self, channel_id: str, *, channel: str, credential_scope: str, binding_count: int
    ) -> None:
        now = time.time()
        current = self._channels.get(channel_id)
        if current is None:
            self._channels[channel_id] = ChannelHealth(
                id=channel_id,
                channel=channel,
                credential_scope=credential_scope,
                binding_count=binding_count,
                started_at=now,
            )
            return
        current.channel = channel
        current.credential_scope = credential_scope
        current.binding_count = binding_count
        current.state = "starting"
        current.started_at = now

    def connected(self, channel_id: str) -> None:
        health = self._require(channel_id)
        now = time.time()
        health.state = "connected"
        health.connected_at = now
        health.last_transport_at = now
        health.last_error = None

    def transport_activity(self, channel_id: str) -> None:
        health = self._require(channel_id)
        health.last_transport_at = time.time()

    def inbound(self, channel_id: str) -> None:
        health = self._require(channel_id)
        now = time.time()
        health.last_transport_at = now
        health.last_inbound_at = now

    def error(self, channel_id: str, error: BaseException | str) -> None:
        health = self._require(channel_id)
        health.state = "degraded"
        health.last_error = str(error)[:500]

    def recovering(self, channel_id: str, reason: str) -> None:
        health = self._require(channel_id)
        health.state = "recovering"
        health.recovery_count += 1
        health.last_error = reason[:500]

    def stopped(self, channel_id: str) -> None:
        self._require(channel_id).state = "stopped"

    def snapshot(self) -> dict[str, object]:
        return {
            "generated_at": time.time(),
            "channels": [
                asdict(health)
                for health in sorted(self._channels.values(), key=lambda item: item.id)
            ],
        }

    def write(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(self.snapshot(), ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        os.chmod(temporary, 0o600)
        temporary.replace(path)

    def _require(self, channel_id: str) -> ChannelHealth:
        health = self._channels.get(channel_id)
        if health is None:
            raise KeyError(f"unregistered channel health id: {channel_id}")
        return health


async def channel_health_audit_loop(
    registry: ChannelHealthRegistry,
    path: Path,
    stop: asyncio.Event,
    *,
    interval: float = 30.0,
) -> None:
    """Persist a single cross-channel audit surface until bridge shutdown."""
    while not stop.is_set():
        try:
            registry.write(path)
        except OSError:
            log.exception("channel health audit write failed: %s", path)
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            continue
    try:
        registry.write(path)
    except OSError:
        log.exception("final channel health audit write failed: %s", path)
