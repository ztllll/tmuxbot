"""Provider-authored Pi session handoff records.

Pi's native ``/new`` replaces the session in-process.  The new transcript can
exist later than the switch, and several panes may share a cwd, so neither a
route's old pin nor transcript mtime identifies the live replacement safely.
The tiny Pi extension installed with tmuxbot writes one atomic record on each
``session_start``; this adapter validates it before the route adopts it.
"""
from __future__ import annotations

import hashlib
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
    """Return the cross-language, collision-resistant record path for one pane.

    This exact ASCII slug plus SHA-256 suffix is implemented by the provider
    extension too.  Python's ``str.isalnum`` accepts Chinese characters whereas
    JavaScript's original ASCII regex does not; using the same ASCII-only rule
    and a target digest makes a direct `/new` route handoff reliable for every
    tmux session name, including Chinese names.
    """
    safe = "".join(
        char if char.isascii() and (char.isalnum() or char in "._-") else "_"
        for char in tmux_target
    )
    digest = hashlib.sha256(tmux_target.encode("utf-8")).hexdigest()[:16]
    return handoff_directory() / f"{safe}-{digest}.json"


def _legacy_handoff_record_path(tmux_target: str) -> Path:
    """Pre-digest record name, retained solely to migrate loaded old extensions."""
    safe = "".join(
        char if char.isascii() and (char.isalnum() or char in "._-") else "_"
        for char in tmux_target
    )
    return handoff_directory() / f"{safe}.json"


def read_handoff(tmux_target: str, cwd: Path) -> PiHandoff | None:
    """Return a validated provider-authored record for this exact route.

    The record is a hint, not authority: caller still validates the transcript
    header.  Malformed, foreign-target, relative, symlink, or wrong-cwd records
    fail closed and leave the route's durable identity untouched.  The legacy
    filename is read during migration because a pre-reload Pi extension remains
    loaded until its next `/reload` or session restart.
    """
    paths = (handoff_record_path(tmux_target), _legacy_handoff_record_path(tmux_target))
    for path in dict.fromkeys(paths):
        try:
            if path.is_symlink():
                continue
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError):
            continue
        handoff = _validated_handoff(raw, tmux_target, cwd)
        if handoff is not None:
            return handoff
    return None


def _validated_handoff(raw: object, tmux_target: str, cwd: Path) -> PiHandoff | None:
    """Validate a decoded handoff payload without trusting its file location."""
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
