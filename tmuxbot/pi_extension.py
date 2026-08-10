"""Install the managed Pi session-handoff extension without overwriting user extensions."""
from __future__ import annotations

import os
from pathlib import Path

_HANDOFF_EXTENSION = "tmuxbot-session-handoff.ts"


def managed_extension_source() -> Path:
    return Path(__file__).resolve().parent.parent / "pi-extensions" / _HANDOFF_EXTENSION


def install_pi_handoff_extension() -> Path:
    """Atomically install the small provider-side handoff reporter.

    Pi auto-discovers global extensions from this directory.  The source is
    versioned with tmuxbot; the installed copy is private and independent of a
    project checkout's cwd.
    """
    source = managed_extension_source()
    if not source.is_file():
        raise OSError(f"managed Pi handoff extension missing: {source}")
    target_dir = Path.home() / ".pi" / "agent" / "extensions"
    target_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(target_dir, 0o700)
    target = target_dir / _HANDOFF_EXTENSION
    content = source.read_bytes()
    if target.exists() and target.is_file() and target.read_bytes() == content:
        return target
    temporary = target.with_suffix(target.suffix + ".tmp")
    try:
        temporary.write_bytes(content)
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            temporary.unlink()
    return target
