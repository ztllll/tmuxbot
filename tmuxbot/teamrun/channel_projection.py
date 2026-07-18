"""Project high-signal TeamRun events to the IM binding that owns the CLI."""

from __future__ import annotations

import asyncio
import html
import logging
from collections.abc import Iterable

from tmuxbot.control_plane.models import RunEvent
from tmuxbot.control_plane.repository import ControlPlaneRepository


log = logging.getLogger(__name__)

_PROJECTED_EVENTS = {
    "teamrun.running", "teamrun.completed", "teamrun.operator_required",
    "teamtask.working", "teamtask.review_requested", "teamtask.review_approved",
    "teamtask.review_rejected", "teamtask.blocked", "teamtask.dispatch_uncertain",
    "teamtask.worktree_prepared", "teamtask.worktree_merged",
}
_LABELS = {
    "teamrun.running": "协作已启动", "teamrun.completed": "协作已完成",
    "teamrun.operator_required": "协作需要人工处理", "teamtask.working": "CLI 开始执行任务",
    "teamtask.review_requested": "任务已交给独立审查", "teamtask.review_approved": "审查通过",
    "teamtask.review_rejected": "审查退回修改", "teamtask.blocked": "任务已阻塞",
    "teamtask.dispatch_uncertain": "tmux 投递需要人工确认",
    "teamtask.worktree_prepared": "已创建隔离 worktree", "teamtask.worktree_merged": "隔离成果已合并",
}


async def projection_loop(
    repository: ControlPlaneRepository,
    frontends: Iterable[object],
    *,
    interval_seconds: float = 2.0,
) -> None:
    cursor = _latest_sequence(repository)
    while True:
        for event in repository.list_events(after_sequence=cursor, limit=500):
            cursor = event.sequence or cursor
            if event.event_type not in _PROJECTED_EVENTS:
                continue
            await _project_event(repository, frontends, event)
        await asyncio.sleep(interval_seconds)


def _latest_sequence(repository: ControlPlaneRepository) -> int:
    cursor = 0
    while events := repository.list_events(after_sequence=cursor, limit=500):
        next_cursor = events[-1].sequence or cursor
        if next_cursor == cursor:
            return cursor
        cursor = next_cursor
    return cursor


async def _project_event(repository: ControlPlaneRepository, frontends: Iterable[object], event: RunEvent) -> None:
    if event.aggregate_type == "team_run":
        run_id = event.aggregate_id
    else:
        run_id = str(event.payload.get("run_id") or "")
    if not run_id:
        return
    try:
        snapshot = repository.get_team_run(run_id)
    except KeyError:
        return
    targets = set()
    for agent in snapshot.agents:
        managed = repository.get_managed_session(agent.managed_session_id)
        if managed is not None:
            targets.add(f"{managed.tmux_session}:{managed.tmux_window}.{managed.tmux_pane}")
    body = _render_event(event, snapshot.run.goal)
    for frontend in frontends:
        for binding in getattr(frontend, "bindings", []):
            if binding.tmux_target not in targets:
                continue
            try:
                await frontend.send_html(binding.chat_id, binding.thread_id, body)
            except Exception:
                log.exception("teamrun channel projection failed run=%s binding=%s", run_id, binding.name)


def _render_event(event: RunEvent, goal: str) -> str:
    label = _LABELS[event.event_type]
    task = event.aggregate_id if event.aggregate_type == "team_task" else None
    parts = [f"🤝 <b>协作更新</b> · {html.escape(label)}", f"目标：{html.escape(goal)}"]
    if task:
        parts.append(f"任务：<code>{html.escape(task)}</code>")
    if event.event_type == "teamtask.dispatch_uncertain":
        parts.append("⚠️ 请在 WebUI 确认 tmux 是否已收到任务，系统不会自动重发。")
    parts.append("完整审计：WebUI → TeamRun 协作台")
    return "\n".join(parts)
