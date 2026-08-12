"""Validate provider-authored Pi session health records for one exact route."""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class PiSessionHealth:
    tmux_target: str
    cwd: Path
    session_id: str
    transcript_path: Path
    state: str
    observed_at: str
    error_message: str | None = None
    response_id: str | None = None


def session_health_directory() -> Path:
    state_dir = Path(
        os.getenv("TMUXBOT_STATE_DIR") or Path.home() / ".local" / "state" / "tmuxbot"
    ).expanduser()
    return state_dir / "pi-session-health"


def session_health_record_path(tmux_target: str) -> Path:
    safe = "".join(
        char if char.isascii() and (char.isalnum() or char in "._-") else "_"
        for char in tmux_target
    )
    digest = hashlib.sha256(tmux_target.encode("utf-8")).hexdigest()[:16]
    return session_health_directory() / f"{safe}-{digest}.json"


def read_session_health(tmux_target: str, cwd: Path) -> PiSessionHealth | None:
    """Return a fail-closed, exact-target/cwd/session-header health record."""
    path = session_health_record_path(tmux_target)
    try:
        if path.is_symlink():
            return None
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return None
    if not isinstance(raw, dict) or raw.get("tmuxTarget") != tmux_target:
        return None
    values = tuple(raw.get(key) for key in ("cwd", "sessionId", "transcriptPath", "state", "observedAt"))
    if not all(isinstance(value, str) and value for value in values):
        return None
    claimed_cwd = Path(values[0]).expanduser()
    transcript = Path(values[2]).expanduser()
    if not claimed_cwd.is_absolute() or not transcript.is_absolute():
        return None
    if values[3] not in {"idle", "working", "recovering", "terminal_error"}:
        return None
    try:
        expected_cwd = cwd.expanduser().resolve()
        if claimed_cwd.resolve() != expected_cwd:
            return None
        if transcript.is_symlink() or not transcript.is_file():
            return None
        header = None
        with transcript.open("r", encoding="utf-8", errors="replace") as stream:
            for _ in range(32):
                line = stream.readline()
                if not line:
                    break
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(row, dict) and row.get("type") == "session":
                    header = row
                    break
        if header is None or str(header.get("id") or "") != values[1]:
            return None
        header_cwd = Path(str(header.get("cwd") or "")).expanduser().resolve()
        if header_cwd != expected_cwd:
            return None
    except OSError:
        return None
    error = raw.get("error")
    error_message = response_id = None
    if isinstance(error, dict):
        message = error.get("message")
        native_id = error.get("responseId")
        error_message = message[:500] if isinstance(message, str) and message else None
        response_id = native_id if isinstance(native_id, str) and native_id else None
    if values[3] == "terminal_error" and error_message is None:
        return None
    return PiSessionHealth(
        tmux_target=tmux_target,
        cwd=expected_cwd,
        session_id=values[1],
        transcript_path=transcript,
        state=values[3],
        observed_at=values[4],
        error_message=error_message,
        response_id=response_id,
    )
