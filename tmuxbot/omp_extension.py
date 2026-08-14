"""Resolve the packaged managed OMP extension loaded by each TUI launch."""

from __future__ import annotations

from pathlib import Path

_HANDOFF_EXTENSION = "tmuxbot-session-handoff.ts"


def managed_extension_source() -> Path:
    """Return the verified absolute extension path from a wheel or source checkout."""

    package_source = Path(__file__).resolve().parent / "omp-extensions" / _HANDOFF_EXTENSION
    checkout_source = Path(__file__).resolve().parent.parent / "omp-extensions" / _HANDOFF_EXTENSION
    for candidate in (package_source, checkout_source):
        resolved = candidate.resolve()
        if resolved.is_file():
            return resolved
    raise FileNotFoundError(
        f"managed OMP handoff extension missing; checked {package_source} and {checkout_source}"
    )
