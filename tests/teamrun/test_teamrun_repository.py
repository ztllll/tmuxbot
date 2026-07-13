import sqlite3
from datetime import datetime, timezone

import pytest

from tmuxbot.control_plane.repository import ControlPlaneRepository
from tmuxbot.teamrun.domain import (
    AgentRole,
    TeamAgent,
    TeamRun,
    TeamRunState,
    TeamTask,
    TeamTaskState,
)


NOW = datetime(2026, 7, 11, 12, 0, tzinfo=timezone.utc)


def make_run() -> TeamRun:
    return TeamRun(
        run_id="run-1",
        goal="实现并审查确定性调度",
        state=TeamRunState.DRAFT,
        max_retries=1,
        created_at=NOW,
        updated_at=NOW,
    )


def make_agents() -> list[TeamAgent]:
    return [
        TeamAgent("coordinator", "run-1", AgentRole.COORDINATOR, "tmux-coordinator"),
        TeamAgent("implementer", "run-1", AgentRole.IMPLEMENTER, "tmux-implementer"),
        TeamAgent("reviewer", "run-1", AgentRole.REVIEWER, "tmux-reviewer"),
    ]


def make_tasks() -> list[TeamTask]:
    return [
        TeamTask(
            task_id="implement",
            run_id="run-1",
            title="实现",
            goal="实现功能并提交证据",
            role=AgentRole.IMPLEMENTER,
            state=TeamTaskState.PENDING,
            dependencies=(),
            requires_write=True,
            max_attempts=2,
            attempt=0,
            assignee_agent_id=None,
            created_at=NOW,
            updated_at=NOW,
        ),
        TeamTask(
            task_id="follow-up",
            run_id="run-1",
            title="后续",
            goal="依赖实现验收",
            role=AgentRole.IMPLEMENTER,
            state=TeamTaskState.PENDING,
            dependencies=("implement",),
            requires_write=True,
            max_attempts=2,
            attempt=0,
            assignee_agent_id=None,
            created_at=NOW,
            updated_at=NOW,
        ),
    ]


def test_migration_adds_teamrun_tables_and_partial_write_lease_index(tmp_path):
    path = tmp_path / "control.sqlite3"
    ControlPlaneRepository(path).migrate()

    with sqlite3.connect(path) as db:
        tables = {
            row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        indexes = {
            row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='index'")
        }

    assert {
        "team_runs",
        "team_agents",
        "team_tasks",
        "mailbox_messages",
        "artifacts",
        "write_leases",
    } <= tables
    assert "write_leases_one_active_per_run" in indexes


def test_repository_creates_and_loads_teamrun_graph_idempotently(tmp_path):
    repo = ControlPlaneRepository(tmp_path / "control.sqlite3")
    repo.migrate()

    assert repo.create_team_run(
        make_run(), make_agents(), make_tasks(), event_id="request-create-1"
    ) is True
    assert repo.create_team_run(
        make_run(), make_agents(), make_tasks(), event_id="request-create-1"
    ) is False

    snapshot = repo.get_team_run("run-1")
    assert snapshot.run.goal == "实现并审查确定性调度"
    assert [agent.role for agent in snapshot.agents] == [
        AgentRole.COORDINATOR,
        AgentRole.IMPLEMENTER,
        AgentRole.REVIEWER,
    ]
    assert snapshot.tasks[1].dependencies == ("implement",)
    assert len(repo.list_events(after_sequence=0, limit=20)) == 1


def test_repository_detects_a_managed_session_used_by_an_active_run(tmp_path):
    repo = ControlPlaneRepository(tmp_path / "control.sqlite3")
    repo.migrate()
    repo.create_team_run(make_run(), make_agents(), make_tasks(), event_id="create")

    assert repo.has_active_teamrun_for_managed_session("tmux-implementer") is True
    assert repo.has_active_teamrun_for_managed_session("other-session") is False
    repo.set_team_run_state(
        "run-1", allowed={TeamRunState.DRAFT}, state=TeamRunState.COMPLETED,
        event_id="complete", now=NOW,
    )
    assert repo.has_active_teamrun_for_managed_session("tmux-implementer") is False


def test_repository_enforces_one_active_writer_per_run(tmp_path):
    repo = ControlPlaneRepository(tmp_path / "control.sqlite3")
    repo.migrate()
    repo.create_team_run(make_run(), make_agents(), make_tasks(), event_id="create")

    assert repo.acquire_write_lease("lease-1", "run-1", "implement", now=NOW) is True
    assert repo.acquire_write_lease("lease-2", "run-1", "follow-up", now=NOW) is False
    repo.release_write_lease("run-1", "implement", now=NOW)
    assert repo.acquire_write_lease("lease-2", "run-1", "follow-up", now=NOW) is True


def test_repository_rejects_duplicate_role_and_cross_run_dependency(tmp_path):
    repo = ControlPlaneRepository(tmp_path / "control.sqlite3")
    repo.migrate()
    duplicate_agents = make_agents() + [
        TeamAgent("other-reviewer", "run-1", AgentRole.REVIEWER, "tmux-reviewer-2")
    ]

    with pytest.raises(sqlite3.IntegrityError):
        repo.create_team_run(make_run(), duplicate_agents, make_tasks(), event_id="bad")


def test_task_ids_are_scoped_to_their_teamrun(tmp_path):
    repo = ControlPlaneRepository(tmp_path / "control.sqlite3")
    repo.migrate()
    repo.create_team_run(make_run(), make_agents(), make_tasks(), event_id="create-1")
    second_run = TeamRun("run-2", "second", TeamRunState.DRAFT, 1, NOW, NOW)
    second_agents = [
        TeamAgent("run-2:coordinator", "run-2", AgentRole.COORDINATOR, "r2-coord"),
        TeamAgent("run-2:implementer", "run-2", AgentRole.IMPLEMENTER, "r2-impl"),
        TeamAgent("run-2:reviewer", "run-2", AgentRole.REVIEWER, "r2-review"),
    ]
    second_tasks = [
        TeamTask(
            task_id="implement",
            run_id="run-2",
            title="same local id",
            goal="second run task",
            role=AgentRole.IMPLEMENTER,
            state=TeamTaskState.PENDING,
            dependencies=(),
            requires_write=True,
            max_attempts=1,
            attempt=0,
            assignee_agent_id=None,
            created_at=NOW,
            updated_at=NOW,
        )
    ]

    assert repo.create_team_run(
        second_run, second_agents, second_tasks, event_id="create-2"
    ) is True
    assert repo.get_team_task("run-1", "implement").goal != repo.get_team_task(
        "run-2", "implement"
    ).goal
