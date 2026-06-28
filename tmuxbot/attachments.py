"""Attachment helpers shared by IM frontends."""
from __future__ import annotations

import html
import mimetypes
import os
import re
from dataclasses import dataclass
from pathlib import Path


ATTACHMENT_DIR = Path(os.getenv("TMUXBOT_ATTACHMENT_DIR", "/tmp/tmuxbot-attachments"))
_UNSAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._ -]+")
_IMAGE_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    ".gif",
    ".bmp",
    ".ico",
    ".tif",
    ".tiff",
    ".heic",
}


@dataclass(frozen=True)
class OutboundAttachment:
    path: Path
    kind: str


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
    backend_name: str | None = None,
) -> str:
    """Build the TUI prompt that references downloaded files with @path."""
    clean_caption = (caption or "").strip() or default_caption
    if (backend_name or "").lower() == "codex":
        return _codex_attachment_prompt(clean_caption, paths)

    refs = "\n".join(f"@{Path(p)}" for p in paths)
    return f"{clean_caption}\n\n{refs}"


def _codex_attachment_prompt(caption: str, paths: list[str | Path]) -> str:
    images = [Path(p) for p in paths if is_image_file(p)]
    files = [Path(p) for p in paths if not is_image_file(p)]
    parts = [caption, ""]

    if images:
        parts.append("请使用 `view_image` 工具查看这些本地图片路径:")
        parts.extend(str(p) for p in images)
    if images and files:
        parts.append("")
    if files:
        parts.append("请使用可用的文件读取工具或命令行读取这些本地文件路径:")
        parts.extend(str(p) for p in files)

    return "\n".join(parts)


def is_image_file(path: str | Path) -> bool:
    p = Path(path)
    if p.suffix.lower() in _IMAGE_EXTENSIONS:
        return True
    mime, _ = mimetypes.guess_type(str(p))
    return bool(mime and mime.startswith("image/"))


def split_outbound_attachments(text: str) -> tuple[str, list[OutboundAttachment]]:
    """Remove local attachment path lines from text and return files to send.

    Only existing local files are extracted. Non-existent paths remain in text so
    normal assistant explanations are not silently altered.
    """
    kept: list[str] = []
    attachments: list[OutboundAttachment] = []
    seen: set[Path] = set()

    for line in text.splitlines():
        path = _attachment_path_from_line(line)
        if path is None:
            kept.append(line)
            continue
        resolved = path.resolve()
        if resolved not in seen:
            seen.add(resolved)
            attachments.append(
                OutboundAttachment(
                    path=path,
                    kind="image" if is_image_file(path) else "file",
                )
            )

    return "\n".join(kept).strip(), attachments


def _attachment_path_from_line(line: str) -> Path | None:
    raw = html.unescape(line).strip()
    if not raw:
        return None

    candidate = _candidate_path(raw)
    if candidate is None and ":" in raw:
        candidate = _candidate_path(raw.rsplit(":", 1)[1].strip())
    if candidate is None and "：" in raw:
        candidate = _candidate_path(raw.rsplit("：", 1)[1].strip())
    if candidate is None:
        return None

    path = Path(candidate).expanduser()
    if path.is_file():
        return path
    return None


def _candidate_path(text: str) -> str | None:
    s = text.strip().strip("`'\"")
    s = re.sub(r"^[\s│┃║▌▐▏▕┆┊|>›»]+", "", s).strip()
    s = re.sub(r"^(?:[-*]\s+|\d+[.)]\s+)", "", s)
    s = s.strip().strip("`'\"")
    if s.startswith("@file://"):
        s = s[1:]
    if s.startswith("file://"):
        return s.removeprefix("file://")
    if s.startswith("@/"):
        return s[1:]
    if s.startswith("/"):
        return s
    return None
