"""Read OMP v3 branch and native plan-mode state from one exact JSONL."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_PLAN_PATH_RE = re.compile(r"^local://[^\s]+-plan\.md$")


@dataclass(frozen=True, slots=True)
class PlanModeSnapshot:
    status: str
    footer: str
    widget: str
    plan: str | None = None


def read_jsonl_entries(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    entries: list[dict[str, Any]] = []
    for line in lines:
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            entries.append(row)
    return entries


def current_jsonl_branch_from_entries(
    entries: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    branch_entries = [row for row in entries if row.get("type") not in {"title", "session"}]
    by_id = {
        row["id"]: row for row in branch_entries if isinstance(row.get("id"), str) and row["id"]
    }
    current = next(
        (row for row in reversed(branch_entries) if isinstance(row.get("id"), str) and row["id"]),
        None,
    )
    branch: list[dict[str, Any]] = []
    seen: set[str] = set()
    while current is not None:
        current_id = current["id"]
        if current_id in seen:
            break
        seen.add(current_id)
        branch.append(current)
        parent_id = current.get("parentId")
        current = by_id.get(parent_id) if isinstance(parent_id, str) else None
    branch.reverse()
    return branch


def current_jsonl_branch(path: Path) -> list[dict[str, Any]]:
    return current_jsonl_branch_from_entries(read_jsonl_entries(path))


def read_plan_mode_snapshot(path: Path) -> PlanModeSnapshot | None:
    return plan_mode_snapshot_from_branch(current_jsonl_branch(path))


def plan_mode_snapshot_from_branch(
    branch: list[dict[str, Any]],
) -> PlanModeSnapshot | None:
    mode: str | None = None
    for row in branch:
        if row.get("type") == "mode_change" and isinstance(row.get("mode"), str):
            mode = row["mode"]
    if mode != "plan":
        return None

    plan = None
    for row in reversed(branch):
        plan = plan_text_from_write_row(row)
        if plan is not None:
            break
    return PlanModeSnapshot(
        status="active",
        footer="📝 plan active",
        widget=(
            "📝 <b>OMP Plan 模式已启用</b>\n"
            "· 计划正文仅从当前会话写入的 <code>local://*-plan.md</code> 同步。"
        ),
        plan=plan,
    )


def plan_text_from_write_block(block: Any) -> str | None:
    if not isinstance(block, dict) or block.get("type") != "toolCall":
        return None
    if str(block.get("name") or "").lower() != "write":
        return None
    arguments = block.get("arguments")
    if not isinstance(arguments, dict):
        return None
    path = arguments.get("path")
    text = arguments.get("content")
    if (
        isinstance(path, str)
        and _PLAN_PATH_RE.fullmatch(path)
        and isinstance(text, str)
        and text.strip()
    ):
        return text.strip()
    return None


def plan_text_from_write_row(row: dict[str, Any]) -> str | None:
    if row.get("type") != "message":
        return None
    message = row.get("message")
    if not isinstance(message, dict) or message.get("role") != "assistant":
        return None
    content = message.get("content")
    if not isinstance(content, list):
        return None
    for block in reversed(content):
        text = plan_text_from_write_block(block)
        if text is not None:
            return text
    return None
