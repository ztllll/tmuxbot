"""A small tmux Control Mode bridge for one Terminal Wall card.

The browser never attaches as a normal tmux client.  ``tmux -CC`` exposes
pane output and accepts tmux commands over one PTY; ``refresh-client -C`` then
sets only this control client's per-window geometry, leaving SSH/Tabby clients
untouched.
"""
from __future__ import annotations

import asyncio
import errno
import os
import pty
import re
import subprocess
from dataclasses import dataclass

from tmuxbot.web.wall import TERMINAL_MAX_COLS, TERMINAL_MAX_FRAME_BYTES, TERMINAL_MAX_ROWS, TmuxWallError


_OUTPUT = re.compile(rb"^%output (%\d+) (.*)\r?\n$")
_METADATA = re.compile(rb"^([^\t\r\n]+)\t(%\d+)\t(@\d+)\r?\n$")


@dataclass(frozen=True, slots=True)
class ControlTarget:
    session: str
    pane_id: str
    window_id: str


def _run_tmux(argv: list[str]) -> bytes:
    try:
        result = subprocess.run(argv, capture_output=True, check=False, timeout=3)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise TmuxWallError("tmux control command failed") from exc
    if result.returncode:
        raise TmuxWallError("tmux control command failed")
    return result.stdout


def control_target(target: str) -> ControlTarget:
    output = _run_tmux(
        ["tmux", "display-message", "-p", "-t", target, "#{session_name}\t#{pane_id}\t#{window_id}"]
    )
    match = _METADATA.fullmatch(output)
    if match is None:
        raise TmuxWallError("tmux control target was malformed")
    return ControlTarget(*(item.decode("utf-8", errors="replace") for item in match.groups()))


def capture_snapshot(target: str) -> str:
    output = _run_tmux(["tmux", "capture-pane", "-p", "-e", "-t", target])
    return output.decode("utf-8", errors="replace")


def _unescape_output(payload: bytes) -> bytes:
    """Decode tmux control-mode's octal and backslash escapes."""
    output = bytearray()
    index = 0
    while index < len(payload):
        if payload[index] != 92 or index + 1 >= len(payload):  # backslash
            output.append(payload[index])
            index += 1
            continue
        if index + 3 < len(payload) and all(48 <= byte <= 55 for byte in payload[index + 1:index + 4]):
            output.append(int(payload[index + 1:index + 4], 8))
            index += 4
            continue
        escapes = {ord("n"): 10, ord("r"): 13, ord("\\"): 92}
        output.append(escapes.get(payload[index + 1], payload[index + 1]))
        index += 2
    return bytes(output)


class ControlModeTerminal:
    """One browser card's isolated tmux Control Mode client."""

    def __init__(self, master_fd: int, process: subprocess.Popen[bytes], target: str, metadata: ControlTarget) -> None:
        self.master_fd = master_fd
        self.process = process
        self.target = target
        self.metadata = metadata
        self._buffer = bytearray()
        self._output: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._closed = False

    @classmethod
    def open(cls, target: str) -> "ControlModeTerminal":
        metadata = control_target(target)
        master_fd, slave_fd = pty.openpty()
        try:
            environment = os.environ.copy()
            environment["TERM"] = "xterm-256color"
            environment.pop("TMUX", None)
            environment.pop("TMUX_PANE", None)
            process = subprocess.Popen(
                ["tmux", "-CC", "attach-session", "-t", metadata.session],
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                close_fds=True,
                shell=False,
                env=environment,
            )
        except BaseException:
            os.close(master_fd)
            os.close(slave_fd)
            raise
        os.close(slave_fd)
        return cls(master_fd, process, target, metadata)

    @property
    def snapshot(self) -> str:
        return capture_snapshot(self.target)

    async def read(self, _max_bytes: int = TERMINAL_MAX_FRAME_BYTES) -> bytes:
        while True:
            item = await self._output.get()
            if item is None:
                return b""
            return item

    async def pump(self) -> None:
        """Parse control lines and publish only the selected pane's output."""
        try:
            while not self._closed:
                chunk = await asyncio.to_thread(os.read, self.master_fd, TERMINAL_MAX_FRAME_BYTES)
                if not chunk:
                    break
                self._buffer.extend(chunk)
                while b"\n" in self._buffer:
                    line, _, remainder = self._buffer.partition(b"\n")
                    self._buffer = bytearray(remainder)
                    self._handle_line(line + b"\n")
        except OSError as exc:
            if exc.errno != errno.EIO:
                raise
        finally:
            await self._output.put(None)

    def _handle_line(self, line: bytes) -> None:
        match = _OUTPUT.fullmatch(line)
        if match is not None and match.group(1).decode() == self.metadata.pane_id:
            self._output.put_nowait(_unescape_output(match.group(2)))

    async def write(self, data: bytes) -> None:
        # ``send-keys -H`` keeps the browser protocol raw: UTF-8, paste data,
        # escape sequences and terminal mouse reports are passed byte-for-byte.
        for offset in range(0, len(data), 128):
            hex_keys = " ".join(f"{byte:02x}" for byte in data[offset:offset + 128])
            await self._command(f"send-keys -t {self.metadata.pane_id} -H {hex_keys}")

    async def resize(self, rows: int, cols: int, **_unused: object) -> None:
        if not 1 <= rows <= TERMINAL_MAX_ROWS or not 1 <= cols <= TERMINAL_MAX_COLS:
            raise TmuxWallError("control-mode resize is outside supported bounds")
        await self._command(f"refresh-client -C {self.metadata.window_id}:{cols}x{rows}")

    async def release_window_size(self) -> None:
        return None

    async def _command(self, command: str) -> None:
        await asyncio.to_thread(os.write, self.master_fd, (command + "\n").encode())

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


async def open_control_terminal(target: str) -> ControlModeTerminal:
    return await asyncio.to_thread(ControlModeTerminal.open, target)
