"""Safe, task-scoped Git worktree lifecycle for concurrent write workers."""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path


class WorktreeError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class TaskWorktree:
    run_id: str
    task_id: str
    attempt: int
    repository_root: Path
    path: Path
    branch: str


class GitWorktreeManager:
    """Create isolated branches without copying ignored files or credentials."""

    def __init__(self, base_directory: Path) -> None:
        self.base_directory = Path(base_directory).resolve()

    def create(self, *, project_root: Path, run_id: str, task_id: str, attempt: int) -> TaskWorktree:
        if attempt < 1:
            raise ValueError("attempt must be at least 1")
        repository_root = self._git_root(project_root)
        branch = self._branch_name(run_id, task_id, attempt)
        path = self.base_directory / _safe_component(run_id) / _safe_component(task_id) / str(attempt)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        existing = self._existing_worktree(path, repository_root)
        if existing is not None:
            if existing.branch != branch or existing.repository_root != repository_root:
                raise WorktreeError("existing task worktree identity does not match")
            return TaskWorktree(run_id, task_id, attempt, repository_root, path, branch)
        if path.exists():
            raise WorktreeError("worktree path already exists and is not a Git worktree")
        result = self._git(
            repository_root,
            "worktree", "add", "-b", branch, str(path), "HEAD",
        )
        if result.returncode != 0:
            raise WorktreeError(_git_error(result))
        return TaskWorktree(run_id, task_id, attempt, repository_root, path, branch)

    def remove(self, worktree: TaskWorktree) -> None:
        result = self._git(worktree.repository_root, "worktree", "remove", "--force", str(worktree.path))
        if result.returncode != 0:
            raise WorktreeError(_git_error(result))

    def _existing_worktree(self, path: Path, repository_root: Path) -> TaskWorktree | None:
        if not (path / ".git").exists():
            return None
        if self._git_common_dir(path) != self._git_common_dir(repository_root):
            raise WorktreeError("existing worktree belongs to a different repository")
        branch_result = self._git(path, "branch", "--show-current")
        if branch_result.returncode != 0 or not branch_result.stdout.strip():
            raise WorktreeError("existing worktree does not have a branch")
        parts = path.relative_to(self.base_directory).parts
        if len(parts) != 3 or not parts[2].isdigit():
            raise WorktreeError("existing worktree path is not task-scoped")
        return TaskWorktree(
            parts[0], parts[1], int(parts[2]), repository_root, path, branch_result.stdout.strip()
        )

    def _git_root(self, directory: Path) -> Path:
        result = self._git(directory, "rev-parse", "--show-toplevel")
        if result.returncode != 0:
            raise WorktreeError("project must be inside a Git repository")
        return Path(result.stdout.strip()).resolve()

    def _git_common_dir(self, directory: Path) -> Path:
        result = self._git(directory, "rev-parse", "--path-format=absolute", "--git-common-dir")
        if result.returncode != 0:
            raise WorktreeError("unable to identify Git repository")
        return Path(result.stdout.strip()).resolve()

    @staticmethod
    def _branch_name(run_id: str, task_id: str, attempt: int) -> str:
        return f"tmuxbot/{_safe_component(run_id)}/{_safe_component(task_id)}/{attempt}"

    @staticmethod
    def _git(directory: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(directory), *arguments],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )


def _safe_component(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip(".-")
    if not cleaned:
        raise ValueError("worktree identifier must contain a safe character")
    return cleaned[:96]


def _git_error(result: subprocess.CompletedProcess[str]) -> str:
    detail = (result.stderr or result.stdout).strip().splitlines()
    return detail[-1] if detail else "Git worktree operation failed"
