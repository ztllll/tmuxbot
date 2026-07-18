from datetime import datetime, timezone

import pytest

from tmuxbot.control_plane.repository import ControlPlaneRepository
from tmuxbot.teamrun.domain import AgentRole, TeamRunState, TeamTaskState
from tmuxbot.teamrun.scheduler import ArtifactInput, TeamRunScheduler


NOW = datetime(2026, 7, 11, 12, 0, tzinfo=timezone.utc)


class FakeTmuxSender:
    def __init__(self):
        self.calls = []

    def is_registered(self, managed_session_id: str) -> bool:
        return managed_session_id in {
            "tmux-coordinator",
            "tmux-implementer",
            "tmux-reviewer",
        }

    def send(self, managed_session_id: str, envelope: dict) -> None:
        self.calls.append((managed_session_id, envelope))


class FailingTmuxSender(FakeTmuxSender):
    def send(self, managed_session_id: str, envelope: dict) -> None:
        self.calls.append((managed_session_id, envelope))
        raise RuntimeError("tmux transport failed after unknown write boundary")


class FakeWorkspaceFactory:
    def __init__(self):
        self.calls = []

    def prepare(self, *, run_id, task, agent, attempt):
        self.calls.append((run_id, task.task_id, agent.agent_id, attempt))
        return "tmux-isolated-worktree"


def scheduler(tmp_path):
    repo = ControlPlaneRepository(tmp_path / "control.sqlite3")
    repo.migrate()
    sender = FakeTmuxSender()
    service = TeamRunScheduler(repo, sender, clock=lambda: NOW)
    service.create_deterministic_run(
        run_id="run-1",
        goal="实现确定性 TeamRun",
        agents={
            AgentRole.COORDINATOR: "tmux-coordinator",
            AgentRole.IMPLEMENTER: "tmux-implementer",
            AgentRole.REVIEWER: "tmux-reviewer",
        },
        tasks=[
            {
                "task_id": "implement",
                "title": "实现",
                "goal": "修改代码并提供测试证据",
                "dependencies": [],
                "role": "implementer",
                "requires_write": True,
                "max_attempts": 2,
            },
            {
                "task_id": "follow-up",
                "title": "后续",
                "goal": "在实现验收后执行",
                "dependencies": ["implement"],
                "role": "implementer",
                "requires_write": True,
                "max_attempts": 2,
            },
        ],
        idempotency_key="create-1",
    )
    return repo, sender, service


def test_start_dispatches_only_ready_task_through_injected_sender(tmp_path):
    repo, sender, service = scheduler(tmp_path)

    service.start("run-1", idempotency_key="start-1")

    snapshot = repo.get_team_run("run-1")
    assert snapshot.run.state is TeamRunState.RUNNING
    assert snapshot.tasks[0].state is TeamTaskState.WORKING
    assert snapshot.tasks[1].state is TeamTaskState.PENDING
    assert len(sender.calls) == 1
    target, dispatch = sender.calls[0]
    assert target == "tmux-implementer"
    assert dispatch == {
        "protocol_version": "tmuxbot.worker.v1",
        "kind": "task.assignment",
        "message_id": "teamrun:run-1:dispatch:implement:1",
        "run_id": "run-1",
        "task_id": "implement",
        "attempt": 1,
        "assignee_agent_id": "run-1:implementer",
        "role": "implementer",
        "goal": "修改代码并提供测试证据",
        "constraints": ["shared-directory single writer", "publish evidence before review"],
        "dependencies": [],
        "expected_artifacts": ["evidence"],
        "acceptance_criteria": ["publish evidence before review"],
        "idempotency_key": "teamrun:run-1:dispatch:implement:1",
    }
    assert repo.get_active_write_lease("run-1").task_id == "implement"
    dispatch = next(item for item in repo.list_mailbox("run-1") if item.kind == "task_dispatch")
    assert dispatch.delivered_at == NOW


def test_failed_dispatch_is_persisted_as_uncertain_and_never_blindly_resent(tmp_path):
    repo = ControlPlaneRepository(tmp_path / "control.sqlite3")
    repo.migrate()
    sender = FailingTmuxSender()
    service = TeamRunScheduler(repo, sender, clock=lambda: NOW)
    service.create_deterministic_run(
        run_id="uncertain-run",
        goal="do not duplicate a possible write",
        agents={
            AgentRole.COORDINATOR: "tmux-coordinator",
            AgentRole.IMPLEMENTER: "tmux-implementer",
            AgentRole.REVIEWER: "tmux-reviewer",
        },
        tasks=[{
            "task_id": "implement", "title": "implement", "goal": "implement",
            "role": "implementer", "dependencies": [], "requires_write": True, "max_attempts": 1,
        }],
        idempotency_key="create",
    )

    service.start("uncertain-run", idempotency_key="start")
    command = repo.list_dispatch_commands("uncertain-run")[0]
    assert command.state == "uncertain"
    assert len(sender.calls) == 1

    affected = service.reconcile()
    assert affected == ["uncertain-run"]
    assert len(sender.calls) == 1
    assert repo.get_team_run("uncertain-run").tasks[0].state is TeamTaskState.OPERATOR_REQUIRED


def test_writing_task_dispatches_to_its_isolated_workspace_session(tmp_path):
    repo = ControlPlaneRepository(tmp_path / "control.sqlite3")
    repo.migrate()
    sender = FakeTmuxSender()
    factory = FakeWorkspaceFactory()
    service = TeamRunScheduler(repo, sender, clock=lambda: NOW, workspace_factory=factory)
    service.create_deterministic_run(
        run_id="worktree-run", goal="isolated write", agents={
            AgentRole.COORDINATOR: "tmux-coordinator",
            AgentRole.IMPLEMENTER: "tmux-implementer",
            AgentRole.REVIEWER: "tmux-reviewer",
        }, tasks=[{
            "task_id": "write", "title": "write", "goal": "write safely", "role": "implementer",
            "dependencies": [], "requires_write": True, "max_attempts": 1,
        }], idempotency_key="create",
    )

    service.start("worktree-run", idempotency_key="start")

    assert factory.calls == [("worktree-run", "write", "worktree-run:implementer", 1)]
    assert sender.calls[0][0] == "tmux-isolated-worktree"


@pytest.mark.parametrize(
    "role,requires_write",
    [("reviewer", False), ("coordinator", True)],
)
def test_run_creation_rejects_role_capability_mismatch(tmp_path, role, requires_write):
    repo = ControlPlaneRepository(tmp_path / "control.sqlite3")
    repo.migrate()
    service = TeamRunScheduler(repo, FakeTmuxSender(), clock=lambda: NOW)

    with pytest.raises(ValueError, match="role capability"):
        service.create_deterministic_run(
            run_id="invalid-run",
            goal="invalid role",
            agents={
                AgentRole.COORDINATOR: "tmux-coordinator",
                AgentRole.IMPLEMENTER: "tmux-implementer",
                AgentRole.REVIEWER: "tmux-reviewer",
            },
            tasks=[{
                "task_id": "invalid",
                "title": "invalid",
                "goal": "invalid",
                "role": role,
                "dependencies": [],
                "requires_write": requires_write,
                "max_attempts": 1,
            }],
            idempotency_key="invalid",
        )


def test_run_creation_rejects_unregistered_browser_session_id(tmp_path):
    repo = ControlPlaneRepository(tmp_path / "control.sqlite3")
    repo.migrate()
    service = TeamRunScheduler(repo, FakeTmuxSender(), clock=lambda: NOW)

    with pytest.raises(ValueError, match="registered managed session"):
        service.create_deterministic_run(
            run_id="invalid-session-run",
            goal="must resolve server-side ids",
            agents={
                AgentRole.COORDINATOR: "tmux-coordinator",
                AgentRole.IMPLEMENTER: "browser-supplied-raw-target",
                AgentRole.REVIEWER: "tmux-reviewer",
            },
            tasks=[{
                "task_id": "implement",
                "title": "implement",
                "goal": "implement",
                "role": "implementer",
                "dependencies": [],
                "requires_write": True,
                "max_attempts": 1,
            }],
            idempotency_key="invalid-session",
        )


def test_worker_completion_only_enters_review_and_is_idempotent(tmp_path):
    repo, sender, service = scheduler(tmp_path)
    service.start("run-1", idempotency_key="start-1")
    artifacts = [ArtifactInput("test", "pytest://331-passed", {"passed": 331})]

    first = service.complete_task(
        "run-1",
        "implement",
        agent_id="run-1:implementer",
        artifacts=artifacts,
        idempotency_key="complete-1",
    )
    second = service.complete_task(
        "run-1",
        "implement",
        agent_id="run-1:implementer",
        artifacts=artifacts,
        idempotency_key="complete-1",
    )

    assert first.state is TeamTaskState.REVIEW
    assert second.state is TeamTaskState.REVIEW
    assert repo.get_active_write_lease("run-1") is None
    assert len(repo.list_artifacts("run-1", "implement")) == 1
    review_messages = [item for item in repo.list_mailbox("run-1") if item.kind == "review_requested"]
    assert len(review_messages) == 1


def test_only_independent_reviewer_can_accept_and_unlock_dependency(tmp_path):
    repo, sender, service = scheduler(tmp_path)
    service.start("run-1", idempotency_key="start-1")
    service.complete_task(
        "run-1",
        "implement",
        agent_id="run-1:implementer",
        artifacts=[ArtifactInput("commit", "git:abc123", {})],
        idempotency_key="complete-1",
    )

    with pytest.raises(ValueError, match="independent reviewer"):
        service.review_task(
            "run-1",
            "implement",
            reviewer_agent_id="run-1:implementer",
            verdict="approved",
            notes="self approval",
            idempotency_key="review-self",
        )

    accepted = service.review_task(
        "run-1",
        "implement",
        reviewer_agent_id="run-1:reviewer",
        verdict="approved",
        notes="evidence verified",
        idempotency_key="review-1",
    )

    assert accepted.state is TeamTaskState.ACCEPTED
    assert repo.get_team_run("run-1").tasks[1].state is TeamTaskState.WORKING
    assert [call[1]["task_id"] for call in sender.calls] == [
        "implement",
        "implement",
        "follow-up",
    ]
    assert sender.calls[1][1]["kind"] == "review.requested"
    assert sender.calls[1][1]["reviewer_agent_id"] == "run-1:reviewer"


def test_non_writing_coordinator_task_can_complete_before_implementation(tmp_path):
    repo = ControlPlaneRepository(tmp_path / "control.sqlite3")
    repo.migrate()
    sender = FakeTmuxSender()
    service = TeamRunScheduler(repo, sender, clock=lambda: NOW)
    service.create_deterministic_run(
        run_id="run-plan-first",
        goal="先制定方案，再实施",
        agents={
            AgentRole.COORDINATOR: "tmux-coordinator",
            AgentRole.IMPLEMENTER: "tmux-implementer",
            AgentRole.REVIEWER: "tmux-reviewer",
        },
        tasks=[
            {
                "task_id": "plan",
                "title": "制定方案",
                "goal": "输出实施计划",
                "role": "coordinator",
                "dependencies": [],
                "requires_write": False,
                "max_attempts": 1,
            },
            {
                "task_id": "implement",
                "title": "实施",
                "goal": "按方案修改代码",
                "role": "implementer",
                "dependencies": ["plan"],
                "requires_write": True,
                "max_attempts": 1,
            },
        ],
        idempotency_key="plan-first",
    )

    service.start("run-plan-first", idempotency_key="start")
    assert sender.calls[0][0] == "tmux-coordinator"
    service.complete_task(
        "run-plan-first",
        "plan",
        agent_id="run-plan-first:coordinator",
        artifacts=[ArtifactInput("plan", "docs:plan", {})],
        idempotency_key="complete-plan",
    )
    service.review_task(
        "run-plan-first",
        "plan",
        reviewer_agent_id="run-plan-first:reviewer",
        verdict="approved",
        notes="plan accepted",
        idempotency_key="review-plan",
    )

    assert repo.get_team_run("run-plan-first").tasks[1].state is TeamTaskState.WORKING
    assert sender.calls[-1][0] == "tmux-implementer"


def test_rejected_review_is_bounded_then_requires_operator(tmp_path):
    repo, _, service = scheduler(tmp_path)
    service.start("run-1", idempotency_key="start-1")
    service.complete_task(
        "run-1", "implement", agent_id="run-1:implementer",
        artifacts=[ArtifactInput("test", "pytest://failed", {})],
        idempotency_key="complete-1",
    )

    retried = service.review_task(
        "run-1", "implement", reviewer_agent_id="run-1:reviewer",
        verdict="rejected", notes="missing coverage", idempotency_key="review-1",
    )
    assert retried.state is TeamTaskState.WORKING
    service.complete_task(
        "run-1", "implement", agent_id="run-1:implementer",
        artifacts=[ArtifactInput("test", "pytest://still-failed", {})],
        idempotency_key="complete-2",
    )
    exhausted = service.review_task(
        "run-1", "implement", reviewer_agent_id="run-1:reviewer",
        verdict="rejected", notes="same failure", idempotency_key="review-2",
    )

    assert exhausted.state is TeamTaskState.OPERATOR_REQUIRED
    assert repo.get_team_run("run-1").run.state is TeamRunState.OPERATOR_REQUIRED


def test_assigned_worker_can_report_blocked_without_losing_write_safety(tmp_path):
    repo, _, service = scheduler(tmp_path)
    service.start("run-1", idempotency_key="start-1")

    blocked = service.block_task(
        "run-1",
        "implement",
        agent_id="run-1:implementer",
        reason="missing operator credential",
        idempotency_key="blocked-1",
    )

    assert blocked.state is TeamTaskState.BLOCKED
    assert repo.get_team_run("run-1").run.state is TeamRunState.OPERATOR_REQUIRED
    assert repo.get_active_write_lease("run-1") is None


def test_pause_resume_stop_and_restart_reconciliation_do_not_duplicate_dispatch(tmp_path):
    repo, sender, service = scheduler(tmp_path)
    service.pause("run-1", idempotency_key="pause-1")
    assert repo.get_team_run("run-1").run.state is TeamRunState.PAUSED
    assert sender.calls == []
    service.resume("run-1", idempotency_key="resume-1")
    assert len(sender.calls) == 1

    restarted = TeamRunScheduler(repo, sender, clock=lambda: NOW)
    restarted.reconcile()
    assert len(sender.calls) == 1
    assert repo.get_team_run("run-1").tasks[0].state is TeamTaskState.WORKING

    service.stop("run-1", reason="operator stop", idempotency_key="stop-1")
    assert repo.get_team_run("run-1").run.state is TeamRunState.STOPPED
    assert repo.get_active_write_lease("run-1") is None
