from __future__ import annotations

import os
import re
import subprocess
import time
from collections.abc import Iterable

from tmuxbot.control_plane.models import (
    SessionClass,
    SessionInventoryItem,
    TmuxPaneRecord,
)
from tmuxbot.state import Binding


PANE_ID_FORMAT = "#{pane_id}"
PANE_FIELD_FORMATS = (
    "#{session_name}",
    "#{window_index}",
    "#{pane_index}",
    "#{pane_current_command}",
    "#{pane_current_path}",
    "#{pane_pid}",
)
PANE_ID_PATTERN = re.compile(rb"%\d+")
NO_SERVER_PATTERN = re.compile(rb"no server running on /\S+")


class TmuxInventoryError(RuntimeError):
    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


class TmuxInventory:
    def __init__(self, *, timeout_seconds: float = 3.0) -> None:
        self._timeout_seconds = timeout_seconds

    def list_panes(self) -> list[TmuxPaneRecord]:
        deadline = time.monotonic() + self._timeout_seconds
        pane_output = self._run_tmux(
            ["tmux", "list-panes", "-a", "-F", PANE_ID_FORMAT],
            deadline=deadline,
            allow_no_server=True,
        )
        if pane_output is None:
            return []
        pane_ids = self._parse_pane_ids(pane_output)
        return [self._read_pane(pane_id, deadline=deadline) for pane_id in pane_ids]

    def _run_tmux(
        self,
        command: list[str],
        *,
        deadline: float,
        allow_no_server: bool = False,
        pane_id: str | None = None,
    ) -> bytes | None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TmuxInventoryError(
                "timeout", "tmux inventory command timed out"
            )
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=False,
                check=False,
                timeout=remaining,
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
            stderr = result.stderr if isinstance(result.stderr, bytes) else b""
            stderr = self._remove_optional_final_newline(stderr)
            normalized_stderr = stderr.lower()
            if any(
                marker in normalized_stderr
                for marker in (
                    b"permission denied",
                    b"access denied",
                    b"authentication failed",
                )
            ):
                raise TmuxInventoryError(
                    "permission", "tmux inventory access was denied"
                )
            if (
                allow_no_server
                and b"socket error" not in normalized_stderr
                and NO_SERVER_PATTERN.fullmatch(stderr)
            ):
                return None
            if pane_id is not None and stderr == f"can't find pane: {pane_id}".encode():
                raise TmuxInventoryError(
                    "changed", "tmux pane changed during inventory"
                )
            raise TmuxInventoryError(
                "command_failed",
                "tmux inventory command failed",
            )
        if not isinstance(result.stdout, bytes):
            raise TmuxInventoryError(
                "command_failed", "tmux inventory output was malformed"
            )
        return result.stdout

    def _parse_pane_ids(self, output: bytes) -> list[str]:
        if output == b"":
            return []
        framed = self._remove_required_final_newline(output)
        pane_ids = framed.split(b"\n")
        if not pane_ids or any(
            PANE_ID_PATTERN.fullmatch(pane_id) is None for pane_id in pane_ids
        ):
            raise TmuxInventoryError(
                "command_failed", "tmux inventory output was malformed"
            )
        return [pane_id.decode("ascii") for pane_id in pane_ids]

    def _read_pane(self, pane_id: str, *, deadline: float) -> TmuxPaneRecord:
        raw_fields: list[bytes] = []
        for field_format in PANE_FIELD_FORMATS:
            output = self._run_tmux(
                [
                    "tmux",
                    "display-message",
                    "-t",
                    pane_id,
                    "-p",
                    field_format,
                ],
                deadline=deadline,
                pane_id=pane_id,
            )
            if output is None:
                raise TmuxInventoryError(
                    "command_failed", "tmux inventory command failed"
                )
            raw_fields.append(self._remove_required_final_newline(output))

        session, window, pane, command, cwd, pid = map(os.fsdecode, raw_fields)
        try:
            window_index = int(window)
            pane_index = int(pane)
            pane_pid = int(pid)
        except ValueError as exc:
            raise TmuxInventoryError(
                "command_failed", "tmux inventory output was malformed"
            ) from exc
        return TmuxPaneRecord(
            target=f"{session}:{window_index}.{pane_index}",
            session_name=session,
            window_index=window_index,
            pane_index=pane_index,
            command=command,
            cwd=cwd,
            pid=pane_pid,
        )

    @staticmethod
    def _remove_required_final_newline(output: bytes) -> bytes:
        if not output.endswith(b"\n"):
            raise TmuxInventoryError(
                "command_failed", "tmux inventory output was malformed"
            )
        return output[:-1]

    @staticmethod
    def _remove_optional_final_newline(output: bytes) -> bytes:
        return output[:-1] if output.endswith(b"\n") else output


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
