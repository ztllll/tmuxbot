"""Content-free counters for result-first IM delivery quality."""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
from pathlib import Path
from typing import Mapping

_COUNTERS = (
    "progress_created",
    "progress_edited",
    "progress_finalized",
    "progress_recreated",
    "results_published",
    "duplicate_results_suppressed",
    "attention_published",
    "attachments_published",
    "result_body_chars",
)


def increment(
    registry: dict[str, dict[str, int]], route: str, counter: str, amount: int = 1
) -> None:
    if counter not in _COUNTERS:
        raise ValueError(f"unknown IM delivery counter: {counter}")
    values = registry.setdefault(route, {name: 0 for name in _COUNTERS})
    values[counter] += amount


def snapshot(registry: Mapping[str, Mapping[str, int]]) -> dict:
    routes = {route: dict(values) for route, values in sorted(registry.items())}
    totals = {name: 0 for name in _COUNTERS}
    for values in routes.values():
        for name in _COUNTERS:
            totals[name] += int(values.get(name, 0))
    return {"version": 1, "routes": routes, "totals": totals}


async def audit_loop(
    path: Path,
    registry: Mapping[str, Mapping[str, int]],
    stop: asyncio.Event,
    *,
    interval_seconds: float = 30.0,
) -> None:
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
        except TimeoutError:
            save(path, registry)
    save(path, registry)


def save(path: Path, registry: Mapping[str, Mapping[str, int]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = json.dumps(snapshot(registry), ensure_ascii=False, indent=2) + "\n"
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
            temp_path = Path(handle.name)
        os.chmod(temp_path, 0o600)
        os.replace(temp_path, path)
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()
