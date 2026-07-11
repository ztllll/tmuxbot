from __future__ import annotations

import subprocess
from collections.abc import Iterable

from tmuxbot.control_plane.models import (
    SessionClass,
    SessionInventoryItem,
    TmuxPaneRecord,
)
from tmuxbot.state import Binding


TMUX_FIELD_SEPARATOR = "\x1f"
TMUX_FORMAT = TMUX_FIELD_SEPARATOR.join(
    (
        "#{session_name}",
        "#{window_index}",
        "#{pane_index}",
        "#{pane_current_command}",
        "#{pane_current_path}",
        "#{pane_pid}",
    )
)


class TmuxInventoryError(RuntimeError):
    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


def parse_tmux_rows(output: str) -> list[TmuxPaneRecord]:
    panes: list[TmuxPaneRecord] = []
    for line_number, line in enumerate(output.splitlines(), start=1):
        if not line:
            continue
        fields = line.split(TMUX_FIELD_SEPARATOR)
        if len(fields) != 6:
            raise ValueError(f"malformed tmux row {line_number}: expected 6 fields")
        session, window, pane, command, cwd, pid = fields
        try:
            panes.append(
                TmuxPaneRecord(
                    target=f"{session}:{window}.{pane}",
                    session_name=session,
                    window_index=int(window),
                    pane_index=int(pane),
                    command=command,
                    cwd=cwd,
                    pid=int(pid),
                )
            )
        except ValueError as exc:
            raise ValueError(f"malformed tmux row {line_number}: invalid integer field") from exc
    return panes


class TmuxInventory:
    def __init__(self, *, timeout_seconds: float = 3.0) -> None:
        self._timeout_seconds = timeout_seconds

    def list_panes(self) -> list[TmuxPaneRecord]:
        try:
            result = subprocess.run(
                ["tmux", "list-panes", "-a", "-F", TMUX_FORMAT],
                capture_output=True,
                text=True,
                check=False,
                timeout=self._timeout_seconds,
            )
        except FileNotFoundError as exc:
            raise TmuxInventoryError(
                "unavailable", "tmux executable is unavailable"
            ) from exc
        except PermissionError as exc:
            raise TmuxInventoryError(
                "permission", "tmux inventory access was denied"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise TmuxInventoryError(
                "timeout", "tmux inventory command timed out"
            ) from exc
        except OSError as exc:
            raise TmuxInventoryError(
                "unavailable", "tmux inventory command is unavailable"
            ) from exc
        if result.returncode != 0:
            stderr = (result.stderr or "").lower()
            if "permission denied" in stderr or "access denied" in stderr:
                raise TmuxInventoryError(
                    "permission", "tmux inventory access was denied"
                )
            if any(
                marker in stderr
                for marker in (
                    "no server running",
                    "can't find server",
                    "no tmux server",
                )
            ):
                return []
            raise TmuxInventoryError(
                "command_failed",
                f"tmux inventory command failed with exit status {result.returncode}",
            )
        try:
            return parse_tmux_rows(result.stdout)
        except ValueError as exc:
            raise TmuxInventoryError(
                "command_failed", "tmux inventory output was malformed"
            ) from exc


def classify_inventory(
    panes: Iterable[TmuxPaneRecord],
    bindings: Iterable[Binding],
    *,
    ignored_targets: set[str],
) -> list[SessionInventoryItem]:
    managed = {binding.tmux_target: binding for binding in bindings}
    items: list[SessionInventoryItem] = []
    for pane in panes:
        binding = managed.get(pane.target)
        if binding is not None:
            classification = SessionClass.MANAGED
        elif pane.target in ignored_targets:
            classification = SessionClass.IGNORED
        else:
            classification = SessionClass.ORPHAN
        items.append(
            SessionInventoryItem(
                pane=pane,
                classification=classification,
                binding_name=None if binding is None else binding.name,
                provider=None if binding is None else binding.backend,
            )
        )
    return items
