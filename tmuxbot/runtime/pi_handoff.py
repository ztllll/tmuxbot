"""Provider-authored Pi session handoff records.

Pi's native ``/new`` replaces the session in-process.  The new transcript can
exist later than the switch, and several panes may share a cwd, so neither a
route's old pin nor transcript mtime identifies the live replacement safely.
The tiny Pi extension installed with tmuxbot writes one atomic record on each
``session_start``; this adapter validates it before the route adopts it.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class PiHandoff:
    tmux_target: str
    cwd: Path
    session_id: str
    transcript_path: Path


def handoff_directory() -> Path:
    state_dir = Path(
        os.getenv("TMUXBOT_STATE_DIR") or Path.home() / ".local" / "state" / "tmuxbot"
    ).expanduser()
    return state_dir / "pi-session-handoffs"


def handoff_record_path(tmux_target: str) -> Path:
    # tmux target contains only a small controlled alphabet in normal use, but
    # keep a filesystem-safe deterministic filename independent of user route
    # names and never permit a target to introduce a path separator.
    safe = "".join(char if char.isalnum() or char in "._-" else "_" for char in tmux_target)
    return handoff_directory() / f"{safe}.json"


def read_handoff(tmux_target: str, cwd: Path) -> PiHandoff | None:
    """Return a validated provider-authored record for this exact route.

    The record is a hint, not authority: caller still validates the transcript
    header.  Malformed, foreign-target, relative, symlink, or wrong-cwd records
    fail closed and leave the route's durable identity untouched.
    """
    path = handoff_record_path(tmux_target)
    try:
        if path.is_symlink():
            return None
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return None
    if not isinstance(raw, dict) or raw.get("tmuxTarget") != tmux_target:
        return None
    session_id = raw.get("sessionId")
    transcript_raw = raw.get("transcriptPath")
    cwd_raw = raw.get("cwd")
    if not all(isinstance(value, str) and value for value in (session_id, transcript_raw, cwd_raw)):
        return None
    transcript = Path(transcript_raw).expanduser()
    claimed_cwd = Path(cwd_raw).expanduser()
    if not transcript.is_absolute() or not claimed_cwd.is_absolute():
        return None
    try:
        expected_cwd = cwd.expanduser().resolve()
        if claimed_cwd.resolve() != expected_cwd:
            return None
        if transcript.is_symlink() or not transcript.is_file():
            return None
    except OSError:
        return None
    return PiHandoff(
        tmux_target=tmux_target,
        cwd=expected_cwd,
        session_id=session_id,
        transcript_path=transcript,
    )
