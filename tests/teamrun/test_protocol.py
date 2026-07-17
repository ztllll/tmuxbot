from dataclasses import replace
from datetime import datetime, timezone

import pytest

from tmuxbot.teamrun.domain import AgentRole
from tmuxbot.teamrun.protocol import (
    ArtifactReference,
    ReviewDecision,
    ReviewRequest,
    TaskAssignment,
    WorkerEvent,
    WorkerEventKind,
)


NOW = datetime(2026, 7, 17, 12, 0, tzinfo=timezone.utc)


def assignment() -> TaskAssignment:
    return TaskAssignment(
        message_id="dispatch:run-1:implement:1",
        run_id="run-1",
        task_id="implement",
        attempt=1,
        assignee_agent_id="run-1:implementer",
        role=AgentRole.IMPLEMENTER,
        goal="实现并提供可核验证据",
        constraints=("shared-directory single writer",),
        dependencies=(),
        expected_artifacts=("commit", "test"),
        acceptance_criteria=("测试通过",),
        idempotency_key="dispatch:run-1:implement:1",
    )


def test_assignment_round_trips_as_versioned_structured_command():
    wire = assignment().to_wire()

    assert wire["protocol_version"] == "tmuxbot.worker.v1"
    assert wire["kind"] == "task.assignment"
    assert TaskAssignment.from_wire(wire) == assignment()


@pytest.mark.parametrize(
    "changes, message",
    [
        ({"attempt": 0}, "attempt"),
        ({"acceptance_criteria": ()}, "acceptance_criteria"),
        ({"protocol_version": "v0"}, "protocol"),
    ],
)
def test_assignment_rejects_unsafe_or_ambiguous_contracts(changes, message):
    with pytest.raises(ValueError, match=message):
        replace(assignment(), **changes)


def event(kind: WorkerEventKind, **changes: object) -> WorkerEvent:
    defaults: dict[str, object] = {
        "event_id": f"event:{kind.value}",
        "kind": kind,
        "run_id": "run-1",
        "task_id": "implement",
        "attempt": 1,
        "actor_agent_id": "run-1:implementer",
        "idempotency_key": f"report:{kind.value}",
        "occurred_at": NOW,
    }
    return WorkerEvent(**{**defaults, **changes})


def test_worker_events_round_trip_and_preserve_evidence():
    completed = event(
        WorkerEventKind.TASK_COMPLETED,
        evidence=(ArtifactReference("test", "pytest://455-passed", {"passed": 455}),),
        message="all checks pass",
    )

    assert WorkerEvent.from_wire(completed.to_wire()) == completed


@pytest.mark.parametrize(
    "kind, changes, message",
    [
        (WorkerEventKind.TASK_PROGRESS, {}, "progress_percent"),
        (WorkerEventKind.TASK_COMPLETED, {}, "requires evidence"),
        (WorkerEventKind.ARTIFACT_PUBLISHED, {}, "requires evidence"),
        (WorkerEventKind.TASK_BLOCKED, {}, "blocked message"),
        (WorkerEventKind.REVIEW_COMPLETED, {}, "review_decision"),
        (WorkerEventKind.TASK_PROGRESS, {"progress_percent": 101}, "between"),
    ],
)
def test_worker_event_requires_evidence_or_state_specific_fields(kind, changes, message):
    with pytest.raises(ValueError, match=message):
        event(kind, **changes)


def test_review_event_carries_an_explicit_machine_readable_verdict():
    reviewed = event(
        WorkerEventKind.REVIEW_COMPLETED,
        actor_agent_id="run-1:reviewer",
        review_decision=ReviewDecision.APPROVED,
        message="evidence verified",
    )

    assert reviewed.to_wire()["review_decision"] == "approved"


def test_review_request_hands_evidence_to_an_independent_reviewer():
    request = ReviewRequest(
        message_id="review:run-1:implement:1",
        run_id="run-1",
        task_id="implement",
        attempt=1,
        reviewer_agent_id="run-1:reviewer",
        producer_agent_id="run-1:implementer",
        goal="审查实现",
        artifacts=(ArtifactReference("commit", "git:abc123", {}),),
        idempotency_key="review:run-1:implement:1",
    )

    assert request.to_wire()["kind"] == "review.requested"

    with pytest.raises(ValueError, match="independent"):
        ReviewRequest(
            message_id="invalid-review",
            run_id="run-1",
            task_id="implement",
            attempt=1,
            reviewer_agent_id="run-1:implementer",
            producer_agent_id="run-1:implementer",
            goal="self review",
            artifacts=(ArtifactReference("commit", "git:abc123", {}),),
            idempotency_key="invalid-review",
        )
