"""Validate provider-authored OMP session health sidecars for one exact route."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

from tmuxbot.runtime.omp_handoff import read_session_header, read_sidecar_payload


@dataclass(frozen=True, slots=True)
class OmpSessionHealth:
    tmux_target: str
    cwd: Path
    session_id: str
    transcript_path: Path
    state: str
    observed_at: str
    updated_at: float
    error_message: str | None = None
    response_id: str | None = None


def session_health_directory() -> Path:
    state_dir = Path(
        os.getenv("TMUXBOT_STATE_DIR") or Path.home() / ".local" / "state" / "tmuxbot"
    ).expanduser()
    return state_dir / "omp-session-health"


def session_health_record_path(tmux_target: str) -> Path:
    safe = "".join(
        char if char.isascii() and (char.isalnum() or char in "._-") else "_"
        for char in tmux_target
    )
    digest = hashlib.sha256(tmux_target.encode("utf-8")).hexdigest()[:16]
    return session_health_directory() / f"{safe}-{digest}.json"


def read_session_health(tmux_target: str, cwd: Path) -> OmpSessionHealth | None:
    """Return a fail-closed, exact-target/cwd/session-header health record."""
    record = read_sidecar_payload(session_health_record_path(tmux_target))
    if record is None:
        return None
    raw, updated_at = record
    if raw.get("tmuxTarget") != tmux_target:
        return None
    session_id = raw.get("sessionId")
    transcript_raw = raw.get("transcriptPath")
    cwd_raw = raw.get("cwd")
    state = raw.get("state")
    observed_at = raw.get("observedAt")
    if not all(
        isinstance(value, str) and value
        for value in (session_id, transcript_raw, cwd_raw, state, observed_at)
    ):
        return None
    if state not in {"idle", "working", "recovering", "terminal_error"}:
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
    if (
        read_session_header(
            transcript,
            expected_cwd,
            expected_session_id=session_id,
        )
        is None
    ):
        return None
    error = raw.get("error")
    error_message = response_id = None
    if isinstance(error, dict):
        message = error.get("message")
        native_id = error.get("responseId")
        error_message = message[:500] if isinstance(message, str) and message else None
        response_id = native_id if isinstance(native_id, str) and native_id else None
    if state == "terminal_error" and error_message is None:
        return None
    return OmpSessionHealth(
        tmux_target=tmux_target,
        cwd=expected_cwd,
        session_id=session_id,
        transcript_path=transcript,
        state=state,
        observed_at=observed_at,
        updated_at=updated_at,
        error_message=error_message,
        response_id=response_id,
    )
