"""Read-only tmux process observations for route lifecycle decisions.

A pane's foreground command is not sufficient to establish route health: a
shell can retain stopped provider children after an interrupted launch.  This
module provides one process-tree observation seam so backends never need to
reconstruct ``ps`` output themselves.
"""
from __future__ import annotations

import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

from tmuxbot.tmux import tmux_pane_pid


@dataclass(frozen=True, slots=True)
class PaneProcess:
    """One live descendant of a tmux pane shell."""

    pid: int
    parent_pid: int
    state: str
    command: str

    @property
    def executable(self) -> str:
        try:
            arguments = shlex.split(self.command)
        except ValueError:
            arguments = self.command.split()
        return Path(arguments[0]).name if arguments else ""

    @property
    def stopped(self) -> bool:
        return "T" in self.state


def pane_processes(target: str) -> tuple[PaneProcess, ...]:
    """Return all pane-shell descendants using a single ``ps`` snapshot."""
    root_pid = tmux_pane_pid(target)
    if root_pid is None:
        return ()
    result = subprocess.run(
        ["ps", "-eo", "pid=,ppid=,stat=,args="], capture_output=True, text=True
    )
    children: dict[int, list[PaneProcess]] = {}
    processes: dict[int, PaneProcess] = {}
    for line in result.stdout.splitlines():
        fields = line.strip().split(maxsplit=3)
        if len(fields) != 4:
            continue
        try:
            pid, parent_pid = int(fields[0]), int(fields[1])
        except ValueError:
            continue
        process = PaneProcess(pid=pid, parent_pid=parent_pid, state=fields[2], command=fields[3])
        processes[pid] = process
        children.setdefault(parent_pid, []).append(process)

    # ``respawn-pane ... exec pi`` makes Pi the pane root rather than a shell
    # descendant.  Include it so health is invariant to the launch wrapper.
    result_processes: list[PaneProcess] = []
    root = processes.get(root_pid)
    if root is not None:
        result_processes.append(root)
    pending = [root_pid]
    while pending:
        parent_pid = pending.pop()
        for process in children.get(parent_pid, ()):
            result_processes.append(process)
            pending.append(process.pid)
    return tuple(result_processes)


def _matching_provider_processes(target: str, executable: str) -> list[PaneProcess]:
    return [process for process in pane_processes(target) if process.executable == executable]


def has_stopped_provider_process(target: str, executable: str) -> bool:
    """Whether a stopped provider descendant makes the pane terminal unsafe."""
    return any(process.stopped for process in _matching_provider_processes(target, executable))


def provider_tree_is_safe(target: str, executable: str) -> bool:
    """Whether the pane has its expected foreground provider and no stopped copy.

    Pi can retain a short wrapper parent plus its worker child, so process count
    is deliberately not a health criterion.  The dangerous state observed in
    production was a stopped sibling, which is unambiguous and fail-closed.
    """
    matching = _matching_provider_processes(target, executable)
    return bool(matching) and not any(process.stopped for process in matching)


def provider_session_file(target: str, executable: str) -> Path | None:
    """Return a provider's precise live transcript path when it publishes one.

    Some Pi releases do not export ``PI_SESSION_FILE``.  Absence is therefore
    not a health failure; callers use it only as an authoritative handoff hint.
    """
    matching = _matching_provider_processes(target, executable)
    if any(process.stopped for process in matching):
        return None
    for process in reversed(matching):
        try:
            raw = Path(f"/proc/{process.pid}/environ").read_bytes()
        except OSError:
            continue
        key = b"PI_SESSION_FILE="
        for entry in raw.split(b"\0"):
            if entry.startswith(key):
                value = entry[len(key) :].decode("utf-8", errors="surrogateescape")
                return Path(value) if value else None
    return None
