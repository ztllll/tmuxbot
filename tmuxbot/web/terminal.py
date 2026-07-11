from __future__ import annotations

import asyncio
import errno
import fcntl
import hashlib
import json
import os
import pty
import secrets
import struct
import subprocess
import termios
import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol
from uuid import uuid4

from tmuxbot.control_plane.models import RunEvent
from tmuxbot.control_plane.repository import ControlPlaneRepository


TERMINAL_TICKET_TTL_SECONDS = 30
TERMINAL_MAX_FRAME_BYTES = 65_536
TERMINAL_MAX_COLS = 500
TERMINAL_MAX_ROWS = 300


def _session_key(token: str) -> bytes:
    return hashlib.sha256(token.encode("utf-8")).digest()


@dataclass(slots=True)
class TerminalTicket:
    token: str
    web_session_key: bytes
    managed_session_id: str
    target: str
    expires_at: int
    consumed: bool = False


class TerminalTicketStore:
    def __init__(self, *, ttl_seconds: int = TERMINAL_TICKET_TTL_SECONDS) -> None:
        self.ttl_seconds = ttl_seconds
        self._tickets: dict[str, TerminalTicket] = {}
        self._lock = threading.Lock()

    def issue(
        self,
        *,
        web_session_token: str,
        managed_session_id: str,
        target: str,
        now: int,
    ) -> TerminalTicket:
        ticket = TerminalTicket(
            token=secrets.token_urlsafe(32),
            web_session_key=_session_key(web_session_token),
            managed_session_id=managed_session_id,
            target=target,
            expires_at=now + self.ttl_seconds,
        )
        with self._lock:
            self._tickets[ticket.token] = ticket
        return ticket

    def consume(
        self, token: str, *, web_session_token: str, now: int
    ) -> TerminalTicket | None:
        candidate_key = _session_key(web_session_token)
        with self._lock:
            ticket = self._tickets.get(token)
            if ticket is None:
                secrets.compare_digest(candidate_key, b"\0" * len(candidate_key))
                return None
            session_matches = secrets.compare_digest(
                candidate_key, ticket.web_session_key
            )
            if (
                not session_matches
                or ticket.consumed
                or now >= ticket.expires_at
            ):
                return None
            ticket.consumed = True
            return ticket


class TakeoverRegistry:
    def __init__(self) -> None:
        self._owners: dict[str, bytes] = {}
        self._lock = threading.Lock()

    def acquire(self, managed_session_id: str, web_session_token: str) -> bool:
        owner = _session_key(web_session_token)
        with self._lock:
            if managed_session_id in self._owners:
                return False
            self._owners[managed_session_id] = owner
            return True

    def release(self, managed_session_id: str, web_session_token: str) -> bool:
        owner = _session_key(web_session_token)
        with self._lock:
            current = self._owners.get(managed_session_id)
            if current is None or not secrets.compare_digest(current, owner):
                return False
            self._owners.pop(managed_session_id, None)
            return True

    def can_input(self, managed_session_id: str, web_session_token: str) -> bool:
        owner = _session_key(web_session_token)
        with self._lock:
            current = self._owners.get(managed_session_id)
            return current is not None and secrets.compare_digest(current, owner)

    def is_active(self, managed_session_id: str) -> bool:
        with self._lock:
            return managed_session_id in self._owners


class TerminalConnection(Protocol):
    async def read(self, max_bytes: int = TERMINAL_MAX_FRAME_BYTES) -> bytes: ...

    async def write(self, data: bytes) -> None: ...

    async def resize(self, rows: int, cols: int) -> None: ...

    async def close(self) -> None: ...


class PtyTerminal:
    def __init__(self, master_fd: int, process: subprocess.Popen) -> None:
        self.master_fd = master_fd
        self.process = process
        self._closed = False

    @classmethod
    def open(cls, target: str) -> "PtyTerminal":
        master_fd, slave_fd = pty.openpty()
        try:
            process = subprocess.Popen(
                ["tmux", "attach-session", "-t", target],
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                close_fds=True,
                shell=False,
            )
        except BaseException:
            os.close(master_fd)
            os.close(slave_fd)
            raise
        os.close(slave_fd)
        return cls(master_fd, process)

    async def read(self, max_bytes: int = TERMINAL_MAX_FRAME_BYTES) -> bytes:
        try:
            return await asyncio.to_thread(os.read, self.master_fd, max_bytes)
        except OSError as exc:
            if exc.errno == errno.EIO:
                return b""
            raise

    async def write(self, data: bytes) -> None:
        await asyncio.to_thread(os.write, self.master_fd, data)

    async def resize(self, rows: int, cols: int) -> None:
        size = struct.pack("HHHH", rows, cols, 0, 0)
        await asyncio.to_thread(
            fcntl.ioctl, self.master_fd, termios.TIOCSWINSZ, size
        )

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            os.close(self.master_fd)
        except OSError:
            pass
        if self.process.poll() is not None:
            return
        self.process.terminate()
        try:
            await asyncio.to_thread(self.process.wait, timeout=2)
        except subprocess.TimeoutExpired:
            self.process.kill()
            await asyncio.to_thread(self.process.wait, timeout=2)


TerminalFactory = Callable[[str], Awaitable[TerminalConnection]]
TargetResolver = Callable[[str], str | None]


async def _default_terminal_factory(target: str) -> TerminalConnection:
    return await asyncio.to_thread(PtyTerminal.open, target)


class TerminalService:
    def __init__(
        self,
        *,
        repository: ControlPlaneRepository,
        target_resolver: TargetResolver,
        allowed_origin: str,
        terminal_factory: TerminalFactory | None = None,
        tickets: TerminalTicketStore | None = None,
        takeovers: TakeoverRegistry | None = None,
    ) -> None:
        self.repository = repository
        self.target_resolver = target_resolver
        self.allowed_origin = allowed_origin.rstrip("/")
        self.terminal_factory = terminal_factory or _default_terminal_factory
        self.tickets = tickets or TerminalTicketStore()
        self.takeovers = takeovers or TakeoverRegistry()
        self._connections: dict[tuple[str, bytes], int] = {}
        self._connections_lock = threading.Lock()

    def resolve_target(self, managed_session_id: str) -> str | None:
        return self.target_resolver(managed_session_id)

    def issue_ticket(
        self, managed_session_id: str, web_session_token: str, *, now: int
    ) -> TerminalTicket | None:
        target = self.resolve_target(managed_session_id)
        if target is None:
            return None
        return self.tickets.issue(
            web_session_token=web_session_token,
            managed_session_id=managed_session_id,
            target=target,
            now=now,
        )

    def consume_ticket(
        self, ticket: str, web_session_token: str, *, now: int
    ) -> TerminalTicket | None:
        consumed = self.tickets.consume(
            ticket, web_session_token=web_session_token, now=now
        )
        if consumed is None:
            return None
        current_target = self.resolve_target(consumed.managed_session_id)
        if current_target != consumed.target:
            return None
        return consumed

    async def open_terminal(self, ticket: TerminalTicket) -> TerminalConnection:
        return await self.terminal_factory(ticket.target)

    def start_takeover(
        self, managed_session_id: str, web_session_token: str
    ) -> str:
        if self.resolve_target(managed_session_id) is None:
            return "missing"
        if not self.is_connected(managed_session_id, web_session_token):
            return "not_connected"
        if not self.takeovers.acquire(managed_session_id, web_session_token):
            return "conflict"
        try:
            self._audit(
                "terminal.takeover.started",
                managed_session_id,
                web_session_token=web_session_token,
                reason="api",
            )
        except BaseException:
            self.takeovers.release(managed_session_id, web_session_token)
            raise
        return "started"

    def end_takeover(
        self,
        managed_session_id: str,
        web_session_token: str,
        *,
        reason: str,
    ) -> bool:
        if not self.takeovers.release(managed_session_id, web_session_token):
            return False
        self._audit(
            "terminal.takeover.ended",
            managed_session_id,
            web_session_token=web_session_token,
            reason=reason,
        )
        return True

    def can_input(self, managed_session_id: str, web_session_token: str) -> bool:
        return self.takeovers.can_input(managed_session_id, web_session_token)

    def connect(self, managed_session_id: str, web_session_token: str) -> None:
        key = (managed_session_id, _session_key(web_session_token))
        with self._connections_lock:
            self._connections[key] = self._connections.get(key, 0) + 1

    def disconnect(self, managed_session_id: str, web_session_token: str) -> None:
        key = (managed_session_id, _session_key(web_session_token))
        with self._connections_lock:
            count = self._connections.get(key, 0)
            if count <= 1:
                self._connections.pop(key, None)
            else:
                self._connections[key] = count - 1

    def is_connected(
        self, managed_session_id: str, web_session_token: str
    ) -> bool:
        key = (managed_session_id, _session_key(web_session_token))
        with self._connections_lock:
            return self._connections.get(key, 0) > 0

    def _audit(
        self,
        event_type: str,
        managed_session_id: str,
        *,
        web_session_token: str,
        reason: str,
    ) -> None:
        now = datetime.now(timezone.utc)
        self.repository.append_event(
            RunEvent(
                event_id=f"{event_type}:{uuid4().hex}",
                event_type=event_type,
                aggregate_type="managed_session",
                aggregate_id=managed_session_id,
                payload={
                    "reason": reason,
                    "operator_session": _session_key(web_session_token)
                    .hex()[:16],
                },
                occurred_at=now,
            )
        )


def parse_resize_message(raw: str) -> tuple[int, int] | None:
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("type") != "resize":
        return None
    rows = payload.get("rows")
    cols = payload.get("cols")
    if (
        not isinstance(rows, int)
        or isinstance(rows, bool)
        or not isinstance(cols, int)
        or isinstance(cols, bool)
        or not 1 <= rows <= TERMINAL_MAX_ROWS
        or not 1 <= cols <= TERMINAL_MAX_COLS
    ):
        return None
    return rows, cols
