from datetime import datetime, timezone

import pytest

from tmuxbot.control_plane.repository import ControlPlaneRepository
from tmuxbot.teamrun.domain import AgentRole, TeamTaskState
from tmuxbot.teamrun.protocol import ArtifactReference, WorkerEvent, WorkerEventKind
from tmuxbot.teamrun.scheduler import TeamRunScheduler
from tmuxbot.teamrun.worker import WorkerReporter
from tmuxbot.teamrun.worker_cli import _event_from_args, add_worker_parser


NOW = datetime(2026, 7, 17, 12, 0, tzinfo=timezone.utc)


class FakeSender:
    def __init__(self):
        self.calls = []

    def is_registered(self, session_id):
        return session_id.startswith("tmux-")

    def send(self, session_id, envelope):
        self.calls.append((session_id, envelope))


def worker(tmp_path):
    repository = ControlPlaneRepository(tmp_path / "control.sqlite3")
    repository.migrate()
    sender = FakeSender()
    scheduler = TeamRunScheduler(repository, sender, clock=lambda: NOW)
    scheduler.create_deterministic_run(
        run_id="run-1",
        goal="worker reports",
        agents={
            AgentRole.COORDINATOR: "tmux-coordinator",
            AgentRole.IMPLEMENTER: "tmux-implementer",
            AgentRole.REVIEWER: "tmux-reviewer",
        },
        tasks=[{
            "task_id": "implement",
            "title": "implement",
            "goal": "implement with evidence",
            "role": "implementer",
            "dependencies": [],
            "requires_write": True,
            "max_attempts": 1,
        }],
        idempotency_key="create",
    )
    scheduler.start("run-1", idempotency_key="start")
    return repository, sender, WorkerReporter(repository, scheduler)


def event(kind, **changes):
    defaults = {
        "event_id": f"worker-event:{kind.value}",
        "kind": kind,
        "run_id": "run-1",
        "task_id": "implement",
        "attempt": 1,
        "actor_agent_id": "run-1:implementer",
        "idempotency_key": f"key:{kind.value}",
        "occurred_at": NOW,
    }
    return WorkerEvent(**{**defaults, **changes})


def test_worker_claim_progress_publish_and_complete_are_structured_and_idempotent(tmp_path):
    repository, sender, reporter = worker(tmp_path)

    reporter.report(event(WorkerEventKind.TASK_CLAIMED))
    reporter.report(event(WorkerEventKind.TASK_PROGRESS, progress_percent=50))
    evidence = ArtifactReference("test", "pytest://468-passed", {"passed": 468})
    reporter.report(event(WorkerEventKind.ARTIFACT_PUBLISHED, evidence=(evidence,)))
    completed = reporter.report(event(WorkerEventKind.TASK_COMPLETED, evidence=(evidence,)))

    assert completed is not None
    assert completed.state is TeamTaskState.REVIEW
    assert len(repository.list_artifacts("run-1", "implement")) == 1
    assert sender.calls[-1][1]["kind"] == "review.requested"
    event_types = [item.event_type for item in repository.list_events(after_sequence=0, limit=50)]
    assert "worker.task.claimed" in event_types
    assert "worker.task.progress" in event_types
    assert "worker.artifact.published" in event_types
    assert "worker.task.completed" in event_types

    retried = reporter.report(event(WorkerEventKind.TASK_COMPLETED, evidence=(evidence,)))
    assert retried is not None
    assert retried.state is TeamTaskState.REVIEW
    assert len(sender.calls) == 2


def test_worker_cannot_report_another_agent_or_attempt(tmp_path):
    _, _, reporter = worker(tmp_path)

    with pytest.raises(ValueError, match="assigned"):
        reporter.report(event(WorkerEventKind.TASK_CLAIMED, actor_agent_id="run-1:reviewer"))
    with pytest.raises(ValueError, match="attempt"):
        reporter.report(event(WorkerEventKind.TASK_CLAIMED, attempt=2))


def test_worker_cli_builds_only_protocol_v1_events():
    import argparse

    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    add_worker_parser(subparsers)
    args = parser.parse_args([
        "worker", "--run", "run-1", "--task", "implement", "--agent", "run-1:implementer",
        "--attempt", "1", "--idempotency-key", "complete-1", "complete",
        "--artifact", "test=pytest://468-passed", "--metadata", '{"passed":468}',
    ])

    event_value = _event_from_args(args)

    assert event_value.kind is WorkerEventKind.TASK_COMPLETED
    assert event_value.to_wire()["protocol_version"] == "tmuxbot.worker.v1"
    assert event_value.evidence[0].metadata == {"passed": 468}
