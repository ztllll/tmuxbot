"""Claude hook command: validate stdin JSON and append it to a local spool."""

from __future__ import annotations

import fcntl
import json
import os
import sys
from pathlib import Path


SUPPORTED_HOOK_EVENTS = frozenset(
    {
        "SessionStart",
        "Notification",
        "MessageDisplay",
        "TaskCreated",
        "TaskCompleted",
        "Stop",
        "StopFailure",
    }
)


def default_hook_spool_path() -> Path:
    from tmuxbot.paths import RuntimePaths

    return RuntimePaths.discover(
        os.environ,
        legacy_project_dir=Path(__file__).resolve().parents[2],
    ).hook_spool_file


def validate_hook_payload(payload: object) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("Claude hook payload must be a JSON object")
    event_name = payload.get("hook_event_name")
    if event_name not in SUPPORTED_HOOK_EVENTS:
        raise ValueError(f"unsupported Claude hook event: {event_name!r}")
    return payload


def append_hook_payload(payload: object, spool_path: Path | None = None) -> None:
    validated = validate_hook_payload(payload)
    path = spool_path or default_hook_spool_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(validated, ensure_ascii=False, separators=(",", ":")) + "\n"
    with open(path, "a", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def read_hook_spool(path: Path, offset: int) -> tuple[list[dict], int]:
    if not path.is_file():
        return [], offset
    records: list[dict] = []
    with open(path, "rb") as handle:
        handle.seek(min(offset, path.stat().st_size))
        data = handle.read()
        new_offset = handle.tell()
    for raw_line in data.splitlines():
        if not raw_line.strip():
            continue
        try:
            records.append(validate_hook_payload(json.loads(raw_line)))
        except (ValueError, json.JSONDecodeError):
            continue
    return records, new_offset


def main() -> int:
    try:
        append_hook_payload(json.load(sys.stdin))
    except Exception as exc:
        print(f"tmuxbot Claude hook ignored: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
