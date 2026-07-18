"""Human-readable TeamRun projections shared by WebUI and IM channels."""

from __future__ import annotations

import html
from pathlib import Path

from tmuxbot.control_plane.repository import ControlPlaneRepository
from tmuxbot.teamrun.domain import TeamRunState


_RUN_LABELS = {
    "draft": "待启动", "running": "运行中", "paused": "已暂停", "completed": "已完成",
    "operator_required": "需要人工处理", "stopped": "已停止", "failed": "失败",
}
_TASK_LABELS = {
    "pending": "等待依赖", "ready": "等待调度", "assigned": "已分配", "working": "工作中",
    "review": "等待审查", "accepted": "已验收", "blocked": "已阻塞",
    "retrying": "准备重试", "operator_required": "需要人工处理", "failed": "失败",
}


def render_latest_teamrun(database_path: Path) -> str:
    repository = ControlPlaneRepository(database_path)
    repository.migrate()
    runs = repository.list_team_runs()
    active = next(
        (item for item in reversed(runs) if item.state not in {TeamRunState.COMPLETED, TeamRunState.STOPPED}),
        None,
    )
    if active is None:
        return "🤝 <b>协作运行</b>\n当前没有进行中的 TeamRun。"
    snapshot = repository.get_team_run(active.run_id)
    lines = [
        "🤝 <b>多 LLM 协作状态</b>",
        f"目标：{html.escape(snapshot.run.goal)}",
        f"状态：<b>{_RUN_LABELS.get(snapshot.run.state.value, snapshot.run.state.value)}</b>"
        f" · <code>{snapshot.run.state.value}</code>",
        "",
    ]
    for task in snapshot.tasks:
        state = _TASK_LABELS.get(task.state.value, task.state.value)
        lines.append(
            f"• <b>{html.escape(task.title)}</b> · {state} · 第 {task.attempt} 次"
        )
    worktrees = repository.list_task_worktrees(snapshot.run.run_id)
    if worktrees:
        lines += ["", f"隔离 worktree：<b>{len(worktrees)}</b> 个"]
        for item in worktrees[-3:]:
            lines.append(f"  └ <code>{html.escape(item.branch)}</code> · {item.state}")
    uncertain = [item for item in repository.list_dispatch_commands(snapshot.run.run_id) if item.state == "uncertain"]
    if uncertain:
        lines += ["", f"⚠️ <b>{len(uncertain)} 个 tmux 投递需要人工确认</b>"]
    lines += ["", "完整审计与合并操作：WebUI → TeamRun 协作台"]
    return "\n".join(lines)
