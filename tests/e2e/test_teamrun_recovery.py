from datetime import datetime, timezone

from tmuxbot.control_plane.repository import ControlPlaneRepository
from tmuxbot.teamrun.domain import AgentRole, TeamRunState, TeamTaskState
from tmuxbot.teamrun.scheduler import TeamRunScheduler


class FakeSender:
    def __init__(self):
        self.calls = []

    def is_registered(self, session_id):
        return session_id in {"coord", "impl", "review"}

    def send(self, session_id, envelope):
        self.calls.append((session_id, envelope))


def test_restart_reconciliation_delivers_persisted_pending_assignment_once(tmp_path):
    now = datetime(2026, 7, 11, tzinfo=timezone.utc)
    repo = ControlPlaneRepository(tmp_path / "control.sqlite3")
    repo.migrate()
    sender = FakeSender()
    scheduler = TeamRunScheduler(repo, sender, clock=lambda: now)
    scheduler.create_deterministic_run(
        run_id="recovery-run",
        goal="prove restart safety",
        agents={
            AgentRole.COORDINATOR: "coord",
            AgentRole.IMPLEMENTER: "impl",
            AgentRole.REVIEWER: "review",
        },
        tasks=[{
            "task_id": "write",
            "title": "write",
            "goal": "write once",
            "role": "implementer",
            "dependencies": [],
            "requires_write": True,
            "max_attempts": 2,
        }],
        idempotency_key="create",
    )
    repo.set_team_run_state(
        "recovery-run",
        allowed={TeamRunState.DRAFT},
        state=TeamRunState.RUNNING,
        event_id="manual-start",
        now=now,
    )
    repo.refresh_task_readiness("recovery-run", now=now)
    repo.claim_team_task("recovery-run", "write", event_id="dispatch-before-crash", now=now)

    restarted = TeamRunScheduler(repo, sender, clock=lambda: now)
    affected = restarted.reconcile()

    snapshot = repo.get_team_run("recovery-run")
    assert affected == []
    assert snapshot.run.state is TeamRunState.RUNNING
    assert snapshot.tasks[0].state is TeamTaskState.WORKING
    assert len(sender.calls) == 1
    assert repo.get_active_write_lease("recovery-run") is not None
