"""Attachment helpers shared by IM frontends."""
from __future__ import annotations

import os
import re
from pathlib import Path


ATTACHMENT_DIR = Path(os.getenv("TMUXBOT_ATTACHMENT_DIR", "/tmp/tmuxbot-attachments"))
_UNSAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._ -]+")


def safe_filename(name: str | None, fallback: str = "attachment.bin") -> str:
    """Return a filesystem-safe basename for downloaded IM attachments."""
    base = Path(name or fallback).name.strip().replace("\x00", "")
    base = _UNSAFE_FILENAME_RE.sub("_", base).strip(" .")
    if not base:
        base = fallback
    if len(base) > 120:
        stem = Path(base).stem[:90].strip(" .") or "attachment"
        suffix = Path(base).suffix[:20]
        base = f"{stem}{suffix}"
    return base


def attachment_path(
    channel: str,
    message_id: str | int,
    key: str | int | None,
    filename: str | None,
) -> Path:
    """Build a stable local path for one downloaded attachment."""
    ATTACHMENT_DIR.mkdir(parents=True, exist_ok=True)
    safe_channel = safe_filename(channel, "im")
    safe_message = safe_filename(str(message_id), "message")
    safe_key = safe_filename(str(key), "file") if key is not None else "file"
    safe_name = safe_filename(filename, "attachment.bin")
    return ATTACHMENT_DIR / f"{safe_channel}_{safe_message}_{safe_key}_{safe_name}"


def attachment_prompt(
    caption: str | None,
    paths: list[str | Path],
    *,
    default_caption: str = "请处理这个文件",
) -> str:
    """Build the TUI prompt that references downloaded files with @path."""
    clean_caption = (caption or "").strip() or default_caption
    refs = "\n".join(f"@{Path(p)}" for p in paths)
    return f"{clean_caption}\n\n{refs}"

