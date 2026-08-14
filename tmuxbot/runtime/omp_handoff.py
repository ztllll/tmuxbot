"""Validate provider-authored OMP session identity sidecars."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path

SIDECAR_VERSION = 1
SUPPORTED_SESSION_VERSIONS = frozenset({1, 2, 3})


@dataclass(frozen=True, slots=True)
class OmpHandoff:
    tmux_target: str
    cwd: Path
    session_id: str
    transcript_path: Path
    updated_at: float


def handoff_directory() -> Path:
    state_dir = Path(
        os.getenv("TMUXBOT_STATE_DIR") or Path.home() / ".local" / "state" / "tmuxbot"
    ).expanduser()
    return state_dir / "omp-session-handoffs"


def handoff_record_path(tmux_target: str) -> Path:
    """Return the exact cross-language record path for one tmux pane."""
    safe = "".join(
        char if char.isascii() and (char.isalnum() or char in "._-") else "_"
        for char in tmux_target
    )
    digest = hashlib.sha256(tmux_target.encode("utf-8")).hexdigest()[:16]
    return handoff_directory() / f"{safe}-{digest}.json"


def read_sidecar_payload(path: Path) -> tuple[dict[str, object], float] | None:
    """Read one current-version regular sidecar without following its final symlink."""
    if not path.is_absolute() or path.is_symlink():
        return None
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
            file_stat = os.fstat(stream.fileno())
            if not stat.S_ISREG(file_stat.st_mode):
                return None
            raw = json.load(stream)
    except (OSError, json.JSONDecodeError, TypeError, UnicodeError):
        return None
    version = raw.get("version") if isinstance(raw, dict) else None
    if not isinstance(raw, dict) or type(version) is not int or version != SIDECAR_VERSION:
        return None
    return raw, file_stat.st_mtime_ns / 1_000_000_000


def read_session_header(
    transcript_path: Path,
    cwd: Path,
    *,
    expected_session_id: str | None = None,
) -> dict[str, object] | None:
    """Validate the first OMP session header in one exact transcript path."""
    if not transcript_path.is_absolute() or transcript_path.is_symlink():
        return None
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        expected_cwd = cwd.expanduser().resolve()
        descriptor = os.open(transcript_path, flags)
        with os.fdopen(descriptor, "r", encoding="utf-8", errors="replace") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                return None
            for _ in range(32):
                line = stream.readline()
                if not line:
                    break
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(row, dict) or row.get("type") != "session":
                    continue
                version = row.get("version")
                session_id = row.get("id")
                header_cwd_raw = row.get("cwd")
                if (
                    type(version) is not int
                    or version not in SUPPORTED_SESSION_VERSIONS
                    or not isinstance(session_id, str)
                    or not session_id
                    or not isinstance(header_cwd_raw, str)
                    or not header_cwd_raw
                ):
                    return None
                header_cwd = Path(header_cwd_raw)
                if not header_cwd.is_absolute() or header_cwd.resolve() != expected_cwd:
                    return None
                if expected_session_id is not None and session_id != expected_session_id:
                    return None
                return row
    except (OSError, RuntimeError):
        return None
    return None


def _process_identity_is_valid(process_id: object, tmux_target: str) -> bool:
    if type(process_id) is not int or process_id <= 0:
        return False

    from tmuxbot.runtime.route_health import pane_processes

    return any(
        process.pid == process_id and not process.stopped for process in pane_processes(tmux_target)
    )


def _pending_transcript_identity_is_valid(
    transcript_path: Path,
    session_id: str,
) -> bool:
    """Validate a provider-authored new-session path before OMP creates the file."""
    if transcript_path.exists() or transcript_path.is_symlink():
        return False
    if "/" in session_id or not transcript_path.name.endswith(f"_{session_id}.jsonl"):
        return False
    try:
        sessions_root = (Path.home() / ".omp" / "agent" / "sessions").resolve()
        transcript_parent = transcript_path.parent.resolve()
    except (OSError, RuntimeError):
        return False
    return transcript_parent.is_relative_to(sessions_root)


def read_handoff(tmux_target: str, cwd: Path) -> OmpHandoff | None:
    """Return a validated current identity record for this exact target and cwd."""
    record = read_sidecar_payload(handoff_record_path(tmux_target))
    if record is None:
        return None
    raw, updated_at = record
    if raw.get("tmuxTarget") != tmux_target:
        return None
    session_id = raw.get("sessionId")
    transcript_raw = raw.get("transcriptPath")
    cwd_raw = raw.get("cwd")
    process_id = raw.get("processId")
    if not _process_identity_is_valid(process_id, tmux_target):
        return None
    if not all(isinstance(value, str) and value for value in (session_id, transcript_raw, cwd_raw)):
        return None
    transcript = Path(transcript_raw)
    claimed_cwd = Path(cwd_raw)
    if not transcript.is_absolute() or not claimed_cwd.is_absolute():
        return None
    try:
        expected_cwd = cwd.expanduser().resolve()
        if claimed_cwd.resolve() != expected_cwd:
            return None
    except (OSError, RuntimeError):
        return None
    header = read_session_header(
        transcript,
        expected_cwd,
        expected_session_id=session_id,
    )
    if header is None and not _pending_transcript_identity_is_valid(transcript, session_id):
        return None
    return OmpHandoff(
        tmux_target=tmux_target,
        cwd=expected_cwd,
        session_id=session_id,
        transcript_path=transcript,
        updated_at=updated_at,
    )
