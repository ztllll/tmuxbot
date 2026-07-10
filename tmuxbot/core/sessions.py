"""Stable identity for a provider session attached to tmux."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SessionIdentity:
    provider: str
    session_id: str
    transcript_path: str | None = None
    tmux_target: str | None = None
    cwd: str | None = None
