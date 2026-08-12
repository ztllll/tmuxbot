"""Restore @narumitw/pi-plan-mode state from one exact Pi JSONL branch."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PLAN_MODE_STATE_TYPE = "plan-mode-state"
PROPOSED_PLAN_MESSAGE_TYPE = "proposed-plan"
PLAN_MODE_COMPLETE_TOOL = "plan_mode_complete"


@dataclass(frozen=True, slots=True)
class PlanModeSnapshot:
    status: str
    footer: str
    widget: str
    plan: str | None = None


def current_jsonl_branch(path: Path) -> list[dict[str, Any]]:
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
        if isinstance(row, dict) and row.get("type") != "session":
            entries.append(row)
    by_id = {
        str(row["id"]): row
        for row in entries
        if isinstance(row.get("id"), str) and row.get("id")
    }
    branch: list[dict[str, Any]] = []
    current = entries[-1] if entries else None
    seen: set[str] = set()
    while current is not None:
        branch.append(current)
        current_id = current.get("id")
        if isinstance(current_id, str):
            if current_id in seen:
                break
            seen.add(current_id)
        parent_id = current.get("parentId")
        current = by_id.get(parent_id) if isinstance(parent_id, str) else None
    branch.reverse()
    return branch


def read_plan_mode_snapshot(path: Path) -> PlanModeSnapshot | None:
    return plan_mode_snapshot_from_branch(current_jsonl_branch(path))


def plan_mode_snapshot_from_branch(
    branch: list[dict[str, Any]],
) -> PlanModeSnapshot | None:
    state: dict[str, Any] | None = None
    for row in reversed(branch):
        if row.get("type") == "custom" and row.get("customType") == PLAN_MODE_STATE_TYPE:
            data = row.get("data")
            if isinstance(data, dict):
                state = data
            break
    if state is None:
        return None

    enabled = state.get("enabled") is True
    latest_plan = _plan_text(state.get("latestPlan")) if enabled else None
    if enabled and latest_plan:
        return PlanModeSnapshot(
            status="ready",
            footer="📝 plan ready",
            widget=(
                "📝 <b>计划已就绪</b>\n"
                "· 使用 <code>/plan</code> 选择实施、保存、导出、继续修改或退出。"
            ),
            plan=latest_plan,
        )
    if enabled:
        return PlanModeSnapshot(
            status="active",
            footer="📝 plan active",
            widget=(
                "📝 <b>Plan Mode 规划中</b>\n"
                "· 当前为只读探索；可用 <code>/plan finalize</code> 请求生成完整计划，"
                "或 <code>/plan exit</code> 退出。"
            ),
        )

    saved = state.get("savedPlan")
    saved_plan = _plan_text(saved.get("plan")) if isinstance(saved, dict) else None
    if saved_plan:
        return PlanModeSnapshot(
            status="saved",
            footer="📝 plan saved",
            widget=(
                "📝 <b>计划已保存</b>\n"
                "· 使用 <code>/plan show</code> 查看、<code>/plan implement</code> 实施，"
                "或 <code>/plan exit</code> 清除。"
            ),
            plan=saved_plan,
        )

    active = state.get("activeImplementation")
    active_plan = _plan_text(active.get("plan")) if isinstance(active, dict) else None
    if active_plan:
        return PlanModeSnapshot(
            status="implementing",
            footer="📝 plan implementing",
            widget=(
                "📝 <b>实施计划生效中</b>\n"
                "· 已批准计划正在约束当前实现；使用 <code>/plan show</code> 查看，"
                "或 <code>/plan exit</code> 清除。"
            ),
            plan=active_plan,
        )
    return None


def _plan_text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None
