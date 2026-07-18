import subprocess

import pytest

from tmuxbot.teamrun.worktree import GitWorktreeManager, WorktreeError


def git(directory, *args):
    return subprocess.run(["git", "-C", str(directory), *args], check=True, capture_output=True, text=True)


def repository(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    git(root, "init", "-q")
    git(root, "config", "user.email", "test@example.com")
    git(root, "config", "user.name", "Test")
    (root / "README.md").write_text("base\n", encoding="utf-8")
    git(root, "add", "README.md")
    git(root, "commit", "-qm", "initial")
    return root


def test_task_worktree_is_branch_isolated_and_idempotent(tmp_path):
    root = repository(tmp_path)
    manager = GitWorktreeManager(tmp_path / "runtime-worktrees")

    created = manager.create(project_root=root, run_id="run 1", task_id="write/ui", attempt=1)
    repeated = manager.create(project_root=root, run_id="run 1", task_id="write/ui", attempt=1)

    assert created == repeated
    assert created.path.is_dir()
    assert created.branch == "tmuxbot/run-1/write-ui/1"
    assert git(created.path, "branch", "--show-current").stdout.strip() == created.branch
    assert git(created.path, "rev-parse", "--show-toplevel").stdout.strip() == str(created.path)


def test_task_worktree_rejects_non_git_project_and_can_be_removed(tmp_path):
    manager = GitWorktreeManager(tmp_path / "runtime-worktrees")
    with pytest.raises(WorktreeError, match="Git repository"):
        manager.create(project_root=tmp_path, run_id="run", task_id="task", attempt=1)

    root = repository(tmp_path)
    created = manager.create(project_root=root, run_id="run", task_id="task", attempt=1)
    manager.remove(created)

    assert not created.path.exists()


def test_task_worktree_merges_only_when_primary_repository_is_clean(tmp_path):
    root = repository(tmp_path)
    manager = GitWorktreeManager(tmp_path / "runtime-worktrees")
    created = manager.create(project_root=root, run_id="run", task_id="task", attempt=1)
    (created.path / "feature.txt").write_text("isolated\n", encoding="utf-8")
    git(created.path, "add", "feature.txt")
    git(created.path, "commit", "-qm", "feature")

    manager.merge_into_repository(created)

    assert (root / "feature.txt").read_text(encoding="utf-8") == "isolated\n"
    (root / "dirty.txt").write_text("dirty\n", encoding="utf-8")
    with pytest.raises(WorktreeError, match="uncommitted"):
        manager.merge_into_repository(created)
