from __future__ import annotations

import subprocess
from collections.abc import Iterable

from tmuxbot.control_plane.models import (
    SessionClass,
    SessionInventoryItem,
    TmuxPaneRecord,
)
from tmuxbot.state import Binding


TMUX_FORMAT = (
    "#{session_name}\t#{window_index}\t#{pane_index}\t"
    "#{pane_current_command}\t#{pane_current_path}\t#{pane_pid}"
)


def parse_tmux_rows(output: str) -> list[TmuxPaneRecord]:
    panes: list[TmuxPaneRecord] = []
    for line_number, line in enumerate(output.splitlines(), start=1):
        if not line:
            continue
        fields = line.split("\t")
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
    def list_panes(self) -> list[TmuxPaneRecord]:
        result = subprocess.run(
            ["tmux", "list-panes", "-a", "-F", TMUX_FORMAT],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            return []
        return parse_tmux_rows(result.stdout)


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
