"""Local tmux window inventory and direct browser terminal relay."""
from __future__ import annotations

import asyncio
import errno
import fcntl
import os
import pty
import re
import struct
import subprocess
import termios
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from tmuxbot.tmux import tmux_error_is_no_server


TERMINAL_MAX_FRAME_BYTES = 65_536
TERMINAL_MAX_COLS = 500
TERMINAL_MAX_ROWS = 300
_TARGET = re.compile(r"^[^:\x00]+:\d+$")


class TmuxWallError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class TmuxWindow:
    target: str
    session_name: str
    window_index: int
    pane_count: int
    commands: tuple[str, ...]
    cwd_summary: str


class TerminalConnection(Protocol):
    async def read(self, max_bytes: int = TERMINAL_MAX_FRAME_BYTES) -> bytes: ...
    async def write(self, data: bytes) -> None: ...
    async def resize(self, rows: int, cols: int, *, apply_window: bool = False) -> None: ...
    async def close(self) -> None: ...


class TmuxWall:
    def __init__(self, *, timeout_seconds: float = 3.0) -> None:
        self.timeout_seconds = timeout_seconds

    def list_windows(self) -> list[TmuxWindow]:
        output = self._run(["tmux", "list-panes", "-a", "-F", "#{session_name}\t#{window_index}\t#{pane_current_command}\t#{pane_current_path}"], allow_no_server=True)
        if output is None or not output:
            return []
        groups: dict[tuple[str, int], list[tuple[str, str]]] = {}
        for line in output.decode(errors="replace").splitlines():
            fields = line.split("\t")
            if len(fields) != 4 or not fields[1].isdigit() or not fields[0]:
                raise TmuxWallError("tmux inventory output was malformed")
            groups.setdefault((fields[0], int(fields[1])), []).append((fields[2], fields[3]))
        return [
            TmuxWindow(
                target=f"{session}:{index}",
                session_name=session,
                window_index=index,
                pane_count=len(panes),
                commands=tuple(sorted({command for command, _ in panes if command})),
                cwd_summary=_summary(sorted({cwd for _, cwd in panes if cwd})),
            )
            for (session, index), panes in sorted(groups.items())
        ]

    def has_window(self, target: str) -> bool:
        return _TARGET.fullmatch(target) is not None and any(item.target == target for item in self.list_windows())

    def window_size(self, target: str) -> tuple[int, int]:
        if _TARGET.fullmatch(target) is None:
            raise TmuxWallError("invalid tmux window target")
        output = self._run(
            ["tmux", "display-message", "-p", "-t", target, "#{window_width}x#{window_height}"]
        )
        if output is None:
            raise TmuxWallError("tmux window is unavailable")
        match = re.fullmatch(rb"(\d+)x(\d+)\n", output)
        if match is None:
            raise TmuxWallError("tmux window size was malformed")
        cols, rows = (int(value) for value in match.groups())
        if not 1 <= cols <= TERMINAL_MAX_COLS or not 1 <= rows <= TERMINAL_MAX_ROWS:
            raise TmuxWallError("tmux window size is outside supported bounds")
        return cols, rows

    def _run(self, argv: list[str], *, allow_no_server: bool = False) -> bytes | None:
        try:
            result = subprocess.run(argv, capture_output=True, check=False, timeout=self.timeout_seconds)
        except FileNotFoundError as exc:
            raise TmuxWallError("tmux executable is unavailable") from exc
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise TmuxWallError("tmux inventory is unavailable") from exc
        if result.returncode:
            stderr = result.stderr.rstrip(b"\n")
            if allow_no_server and tmux_error_is_no_server(stderr):
                return None
            raise TmuxWallError("tmux inventory is unavailable")
        return result.stdout


def _summary(values: list[str]) -> str:
    return "" if not values else values[0] if len(values) == 1 else f"{values[0]} +{len(values) - 1}"


class PtyTerminal:
    def __init__(
        self, master_fd: int, process: subprocess.Popen[bytes], target: str
    ) -> None:
        self.master_fd, self.process, self.target, self.closed = (
            master_fd,
            process,
            target,
            False,
        )
        self.applied_window_size = False

    @classmethod
    def open(cls, target: str) -> "PtyTerminal":
        master_fd, slave_fd = pty.openpty()
        try:
            env = os.environ.copy()
            env["TERM"] = "xterm-256color"
            env.pop("TMUX", None)
            env.pop("TMUX_PANE", None)
            process = subprocess.Popen(["tmux", "attach-session", "-f", "ignore-size", "-t", target], stdin=slave_fd, stdout=slave_fd, stderr=slave_fd, close_fds=True, shell=False, env=env)
        except BaseException:
            os.close(master_fd); os.close(slave_fd)
            raise
        os.close(slave_fd)
        return cls(master_fd, process, target)

    async def read(self, max_bytes: int = TERMINAL_MAX_FRAME_BYTES) -> bytes:
        try:
            return await asyncio.to_thread(os.read, self.master_fd, max_bytes)
        except OSError as exc:
            if exc.errno == errno.EIO:
                return b""
            raise

    async def write(self, data: bytes) -> None:
        await asyncio.to_thread(os.write, self.master_fd, data)

    async def resize(self, rows: int, cols: int, *, apply_window: bool = False) -> None:
        """Resize the browser attach; optionally make it the shared window size.

        A tmux window has exactly one pane layout. The default mirror mode only
        resizes this attach PTY so it cannot change an SSH/Tabby client's layout.
        Explicit takeover uses ``resize-window`` and therefore intentionally
        becomes the single size authority for every client on that window.
        """
        await asyncio.to_thread(
            fcntl.ioctl,
            self.master_fd,
            termios.TIOCSWINSZ,
            struct.pack("HHHH", rows, cols, 0, 0),
        )
        if apply_window:
            await asyncio.to_thread(self._resize_window, rows, cols)
            self.applied_window_size = True

    def _resize_window(self, rows: int, cols: int) -> None:
        try:
            subprocess.run(
                [
                    "tmux",
                    "resize-window",
                    "-t",
                    self.target,
                    "-x",
                    str(cols),
                    "-y",
                    str(rows),
                ],
                capture_output=True,
                check=True,
                timeout=3,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise TmuxWallError("tmux window resize failed") from exc

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            os.close(self.master_fd)
        except OSError:
            pass
        if self.applied_window_size:
            await asyncio.to_thread(self._restore_auto_size)
        if self.process.poll() is None:
            self.process.terminate()
            try:
                await asyncio.to_thread(self.process.wait, timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                await asyncio.to_thread(self.process.wait, timeout=2)

    def _restore_auto_size(self) -> None:
        try:
            subprocess.run(
                ["tmux", "set-window-option", "-u", "-t", self.target, "window-size"],
                capture_output=True,
                check=False,
                timeout=3,
            )
        except OSError:
            pass


TerminalFactory = Callable[[str], Awaitable[TerminalConnection]]


async def open_terminal(target: str) -> TerminalConnection:
    return await asyncio.to_thread(PtyTerminal.open, target)


def parse_resize_message(raw: str) -> tuple[int, int, bool] | None:
    import json
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(value, dict) or value.get("type") != "resize":
        return None
    rows, cols = value.get("rows"), value.get("cols")
    apply_window = value.get("apply_window", False)
    if not isinstance(rows, int) or isinstance(rows, bool) or not isinstance(cols, int) or isinstance(cols, bool) or not isinstance(apply_window, bool) or not 1 <= rows <= TERMINAL_MAX_ROWS or not 1 <= cols <= TERMINAL_MAX_COLS:
        return None
    return rows, cols, apply_window
