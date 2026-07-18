"""Launch one managed CLI inside each isolated task worktree."""

from __future__ import annotations

import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path

from tmuxbot.control_plane.models import ManagedSession, RunEvent
from tmuxbot.control_plane.repository import ControlPlaneRepository
from tmuxbot.providers.adapters import get_provider_adapter
from tmuxbot.teamrun.domain import TeamAgent, TeamTask
from tmuxbot.teamrun.domain import TaskWorktreeRecord
from tmuxbot.teamrun.worktree import GitWorktreeManager, TaskWorktree, WorktreeError


class TaskWorkspaceFactory:
    """Turns a writing task into a separate tmux CLI rooted at its worktree."""

    def __init__(self, repository: ControlPlaneRepository, manager: GitWorktreeManager) -> None:
        self.repository = repository
        self.manager = manager

    def prepare(self, *, run_id: str, task: TeamTask, agent: TeamAgent, attempt: int) -> str:
        source = self.repository.get_managed_session(agent.managed_session_id)
        if source is None:
            raise WorktreeError("source worker session is not registered")
        project = self.repository.get_project(source.project_id)
        provider = self.repository.get_provider_profile(source.provider_id)
        adapter = get_provider_adapter(provider.binary_name) if provider is not None else None
        if project is None or provider is None or adapter is None:
            raise WorktreeError("source worker session has incomplete project or provider")
        worktree = self.manager.create(
            project_root=Path(project.root_path), run_id=run_id, task_id=task.task_id, attempt=attempt
        )
        name = f"worktree:{run_id}:{task.task_id}:{attempt}"
        existing = next((item for item in self.repository.list_managed_sessions() if item.name == name), None)
        if existing is not None:
            return existing.id
        tmux_binary = shutil.which("tmux")
        if tmux_binary is None:
            raise WorktreeError("tmux is unavailable")
        tmux_session = self._tmux_session_name(worktree)
        check = subprocess.run(
            [tmux_binary, "has-session", "-t", tmux_session],
            capture_output=True, text=True, timeout=5, check=False,
        )
        if check.returncode != 0:
            launched = subprocess.run(
                [
                    tmux_binary, "new-session", "-d", "-s", tmux_session, "-c", str(worktree.path),
                    provider.executable_path, *adapter.launch_arguments,
                ],
                capture_output=True, text=True, timeout=10, check=False,
            )
            if launched.returncode != 0:
                raise WorktreeError("unable to launch CLI in isolated worktree")
        managed = ManagedSession(
            id=f"session-{uuid.uuid4().hex}", project_id=project.id, provider_id=provider.id,
            name=name, tmux_session=tmux_session, tmux_window=0, tmux_pane=0,
            status="worktree", created_at=int(time.time()),
        )
        self.repository.create_managed_session(managed)
        self.repository.create_task_worktree(
            TaskWorktreeRecord(
                run_id=run_id, task_id=task.task_id, attempt=attempt,
                path=str(worktree.path), branch=worktree.branch,
                managed_session_id=managed.id, state="active", created_at=task.updated_at,
                released_at=None,
            )
        )
        self.repository.append_event(
            RunEvent(
                event_id=f"teamrun:{run_id}:worktree:{task.task_id}:{attempt}",
                event_type="teamtask.worktree_prepared", aggregate_type="team_task",
                aggregate_id=task.task_id,
                payload={
                    "run_id": run_id, "attempt": attempt, "branch": worktree.branch,
                    "worktree": str(worktree.path), "managed_session_id": managed.id,
                },
                occurred_at=task.updated_at,
            )
        )
        return managed.id

    @staticmethod
    def _tmux_session_name(worktree: TaskWorktree) -> str:
        safe = re.sub(r"[^A-Za-z0-9_-]+", "-", f"{worktree.run_id}-{worktree.task_id}-{worktree.attempt}")
        return f"tmuxbot-wt-{safe[:80]}"
