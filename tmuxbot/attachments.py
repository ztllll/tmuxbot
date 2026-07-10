"""Attachment helpers shared by IM frontends."""
from __future__ import annotations

import html
import mimetypes
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

from tmuxbot.core.messages import AttachmentRef


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
    label: str | None = None


def attachment_ref(
    path: str | Path,
    *,
    kind: str | None = None,
    name: str | None = None,
    mime_type: str | None = None,
) -> AttachmentRef:
    """Describe a downloaded local attachment without channel-specific fields."""
    p = Path(path)
    return AttachmentRef(
        path=str(p),
        kind=kind or ("image" if is_image_file(p) else "file"),
        name=name or p.name,
        mime_type=mime_type or mimetypes.guess_type(str(p))[0],
    )


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


def split_outbound_attachments(
    text: str,
    *,
    cwd: str | Path | None = None,
    allowed_roots: tuple[str | Path, ...] = (),
) -> tuple[str, list[OutboundAttachment]]:
    """Remove local attachment path lines from text and return files to send.

    Only existing local files are extracted. Non-existent paths remain in text so
    normal assistant explanations are not silently altered.
    """
    base_dir = Path(cwd).expanduser().resolve() if cwd is not None else None
    roots = _attachment_roots(base_dir, allowed_roots)
    kept: list[str] = []
    attachments: list[OutboundAttachment] = []
    seen: set[Path] = set()

    for line in text.splitlines():
        whole_path = _attachment_path_from_line(line, cwd=base_dir, roots=roots)
        if whole_path is not None:
            _append_outbound_attachment(attachments, seen, whole_path)
            continue

        rendered = _replace_inline_attachment_links(
            line,
            cwd=base_dir,
            roots=roots,
            attachments=attachments,
            seen=seen,
        )
        kept.append(rendered)

    return "\n".join(kept).strip(), attachments


def prepare_outbound_attachments(
    text: str,
    explicit_paths: tuple[str | Path, ...] = (),
    *,
    cwd: str | Path | None = None,
    allowed_roots: tuple[str | Path, ...] = (),
) -> tuple[str, list[OutboundAttachment]]:
    """Merge trusted structured attachments with paths promoted from reply text."""
    clean_text, discovered = split_outbound_attachments(
        text,
        cwd=cwd,
        allowed_roots=allowed_roots,
    )
    base_dir = Path(cwd).expanduser().resolve() if cwd is not None else None
    attachments: list[OutboundAttachment] = []
    seen: set[Path] = set()

    for raw_path in explicit_paths:
        path = Path(raw_path).expanduser()
        if not path.is_absolute() and base_dir is not None:
            path = base_dir / path
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if resolved.is_file():
            _append_outbound_attachment(attachments, seen, resolved)

    for attachment in discovered:
        _append_outbound_attachment(
            attachments,
            seen,
            attachment.path,
            label=attachment.label,
        )
    return clean_text, attachments


def _attachment_path_from_line(
    line: str,
    *,
    cwd: Path | None,
    roots: tuple[Path, ...],
) -> Path | None:
    raw = html.unescape(line).strip()
    if not raw:
        return None

    candidate = _candidate_path(raw)
    if candidate is None and ":" in raw:
        suffix = raw.rsplit(":", 1)[1].strip()
        if not suffix.startswith(("[", "![")):
            candidate = _candidate_path(suffix)
    if candidate is None and "：" in raw:
        suffix = raw.rsplit("：", 1)[1].strip()
        if not suffix.startswith(("[", "![")):
            candidate = _candidate_path(suffix)
    if candidate is None:
        return None

    return _resolve_attachment_path(candidate, cwd=cwd, roots=roots)


def _candidate_path(text: str) -> str | None:
    s = text.strip().strip("`'\"")
    s = re.sub(r"^[\s│┃║▌▐▏▕┆┊|>›»]+", "", s).strip()
    s = re.sub(r"^(?:[-*]\s+|\d+[.)]\s+)", "", s)
    s = s.strip().strip("`'\"")
    markdown_link = re.fullmatch(r"!?\[[^\]]*\]\(\s*<?([^<>\s][^<>]*?)>?\s*\)", s)
    if markdown_link:
        s = markdown_link.group(1).strip()
    if s.startswith("@file://"):
        s = s[1:]
    if s.startswith("file://"):
        return s.removeprefix("file://")
    if s.startswith("@/"):
        return s[1:]
    if s.startswith("@./") or s.startswith("@../"):
        return s[1:]
    if s.startswith("/"):
        return s
    if s.startswith("./") or s.startswith("../"):
        return s
    return None


_INLINE_MARKDOWN_FILE_RE = re.compile(
    r"!?\[([^\]]*)\]\(\s*(?:<([^>]+)>|([^\)\n]+))\s*\)"
)


def _replace_inline_attachment_links(
    line: str,
    *,
    cwd: Path | None,
    roots: tuple[Path, ...],
    attachments: list[OutboundAttachment],
    seen: set[Path],
) -> str:
    raw = html.unescape(line)

    def replace(match: re.Match[str]) -> str:
        label = match.group(1).strip()
        target = (match.group(2) or match.group(3) or "").strip()
        path = _resolve_attachment_path(target, cwd=cwd, roots=roots)
        if path is None:
            return match.group(0)
        _append_outbound_attachment(attachments, seen, path, label=label or None)
        return label or path.name

    return _INLINE_MARKDOWN_FILE_RE.sub(replace, raw)


def _attachment_roots(
    cwd: Path | None,
    allowed_roots: tuple[str | Path, ...],
) -> tuple[Path, ...]:
    candidates: list[Path] = [ATTACHMENT_DIR, Path(tempfile.gettempdir())]
    if cwd is not None:
        candidates.insert(0, cwd)
    candidates.extend(Path(root).expanduser() for root in allowed_roots)

    roots: list[Path] = []
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved not in roots:
            roots.append(resolved)
    return tuple(roots)


def _resolve_attachment_path(
    candidate: str,
    *,
    cwd: Path | None,
    roots: tuple[Path, ...],
) -> Path | None:
    raw = candidate.strip().strip("`'\"")
    if raw.startswith("file://"):
        raw = raw.removeprefix("file://")
    if raw.startswith("@"):
        raw = raw[1:]

    path = Path(raw).expanduser()
    variants = [path]
    line_suffix = re.sub(r"(?::\d+(?::\d+)?|#L\d+(?:C\d+)?)$", "", raw)
    if line_suffix != raw:
        variants.insert(0, Path(line_suffix).expanduser())

    for variant in variants:
        if not variant.is_absolute():
            if cwd is None:
                continue
            variant = cwd / variant
        try:
            resolved = variant.resolve()
        except OSError:
            continue
        if not resolved.is_file():
            continue
        if not any(resolved == root or resolved.is_relative_to(root) for root in roots):
            continue
        return resolved
    return None


def _append_outbound_attachment(
    attachments: list[OutboundAttachment],
    seen: set[Path],
    path: Path,
    *,
    label: str | None = None,
) -> None:
    resolved = path.resolve()
    if resolved in seen:
        return
    seen.add(resolved)
    attachments.append(
        OutboundAttachment(
            path=resolved,
            kind="image" if is_image_file(resolved) else "file",
            label=label,
        )
    )
