"""Safe serialized input delivery for live CLI processes inside tmux."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Collection


class TmuxBusyTimeout(TimeoutError):
    """Raised when a pane does not become safe for input before the deadline."""


class TmuxRuntime:
    def __init__(
        self,
        *,
        capture_func: Callable[[str, int], str],
        pane_command_func: Callable[[str], str],
        paste_func: Callable[[str, str], Awaitable[None]],
        send_key_func: Callable[[str, str], None],
        busy_detector: Callable[[str], bool],
        sleep_func: Callable[[float], Awaitable[None]] = asyncio.sleep,
        poll_interval: float = 0.25,
        wait_timeout: float = 300.0,
        capture_lines: int = 15,
    ) -> None:
        self._capture = capture_func
        self._pane_command = pane_command_func
        self._paste = paste_func
        self._send_key = send_key_func
        self._is_busy = busy_detector
        self._sleep = sleep_func
        self.poll_interval = poll_interval
        self.wait_timeout = wait_timeout
        self.capture_lines = capture_lines
        self._input_locks: dict[str, asyncio.Lock] = {}

    async def send_text(
        self,
        target: str,
        text: str,
        *,
        with_enter: bool = True,
        expected_commands: Collection[str] | None = None,
    ) -> None:
        lock = self._input_locks.setdefault(target, asyncio.Lock())
        async with lock:
            await self._wait_until_ready(target)
            if expected_commands is not None:
                command = self._pane_command(target)
                if command not in expected_commands:
                    raise RuntimeError(
                        f"tmux pane {target} foreground changed to {command!r} before input"
                    )
            await self._paste(target, text)
            if with_enter:
                self._send_key(target, "Enter")

    async def safe_launch(
        self,
        target: str,
        command: str,
        *,
        allowed_shells: Collection[str],
    ) -> bool:
        foreground = self._pane_command(target)
        if foreground not in allowed_shells:
            return False
        await self.send_text(target, command, expected_commands=allowed_shells)
        return True

    async def _wait_until_ready(self, target: str) -> None:
        elapsed = 0.0
        while True:
            pane = self._capture(target, self.capture_lines)
            if not self._is_busy(pane):
                return
            if elapsed >= self.wait_timeout:
                raise TmuxBusyTimeout(
                    f"tmux pane {target} stayed busy for {self.wait_timeout:.1f}s"
                )
            await self._sleep(self.poll_interval)
            elapsed += self.poll_interval
