from datetime import datetime, timezone

import pytest

from tmuxbot.teamrun.domain import (
    AgentRole,
    TeamTask,
    TeamTaskState,
    validate_task_graph,
)


def task(task_id: str, *dependencies: str) -> TeamTask:
    return TeamTask(
        task_id=task_id,
        run_id="run-1",
        title=task_id,
        goal=f"完成 {task_id}",
        role=AgentRole.IMPLEMENTER,
        state=TeamTaskState.PENDING,
        dependencies=dependencies,
        requires_write=True,
        max_attempts=2,
        attempt=0,
        assignee_agent_id=None,
        created_at=datetime(2026, 7, 11, tzinfo=timezone.utc),
        updated_at=datetime(2026, 7, 11, tzinfo=timezone.utc),
    )


def test_validate_task_graph_accepts_acyclic_dependencies():
    validate_task_graph([task("plan"), task("implement", "plan"), task("verify", "implement")])


def test_validate_task_graph_rejects_cycles_with_stable_error():
    with pytest.raises(ValueError, match="task graph contains a cycle"):
        validate_task_graph([task("a", "b"), task("b", "a")])


def test_validate_task_graph_rejects_unknown_dependency():
    with pytest.raises(ValueError, match="unknown dependency 'missing'"):
        validate_task_graph([task("a", "missing")])
