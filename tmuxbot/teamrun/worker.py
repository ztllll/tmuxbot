"""Worker-side Protocol v1 reports for CLI processes running in tmux."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from tmuxbot.control_plane.models import RunEvent
from tmuxbot.control_plane.repository import ControlPlaneRepository
from tmuxbot.teamrun.domain import AgentRole, TeamTask, TeamTaskState
from tmuxbot.teamrun.protocol import ArtifactReference, WorkerEvent, WorkerEventKind
from tmuxbot.teamrun.scheduler import ArtifactInput, TeamRunScheduler


log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class WorkerReporter:
    """Validate worker reports before applying the permitted TeamRun transition."""

    repository: ControlPlaneRepository
    scheduler: TeamRunScheduler

    def report(self, event: WorkerEvent) -> TeamTask | None:
        log.info(
            "teamrun worker report kind=%s run=%s task=%s attempt=%d actor=%s",
            event.kind.value, event.run_id, event.task_id, event.attempt, event.actor_agent_id,
        )
        task = self._reviewing_task(event) if event.kind is WorkerEventKind.REVIEW_COMPLETED else self._assigned_task(event)
        if not self._append(event):
            log.info("teamrun worker duplicate event=%s", event.event_id)
            return task
        if event.kind is WorkerEventKind.TASK_CLAIMED:
            return task
        if event.kind is WorkerEventKind.TASK_PROGRESS:
            return task
        if event.kind is WorkerEventKind.ARTIFACT_PUBLISHED:
            artifact = event.evidence[0]
            self.repository.publish_team_artifact(
                event.run_id,
                event.task_id,
                agent_id=event.actor_agent_id,
                kind=artifact.kind,
                uri=artifact.uri,
                metadata=dict(artifact.metadata),
                idempotency_key=event.idempotency_key,
                now=event.occurred_at,
            )
            return self.repository.get_team_task(event.run_id, event.task_id)
        if event.kind is WorkerEventKind.TASK_COMPLETED:
            return self.scheduler.complete_task(
                event.run_id,
                event.task_id,
                agent_id=event.actor_agent_id,
                artifacts=[
                    ArtifactInput(item.kind, item.uri, dict(item.metadata))
                    for item in event.evidence
                ],
                idempotency_key=event.idempotency_key,
            )
        if event.kind is WorkerEventKind.TASK_BLOCKED:
            return self.scheduler.block_task(
                event.run_id,
                event.task_id,
                agent_id=event.actor_agent_id,
                reason=event.message or "",
                idempotency_key=event.idempotency_key,
            )
        if event.kind is WorkerEventKind.REVIEW_COMPLETED:
            return self.scheduler.review_task(
                event.run_id,
                event.task_id,
                reviewer_agent_id=event.actor_agent_id,
                verdict=event.review_decision.value if event.review_decision else "",
                notes=event.message or "",
                idempotency_key=event.idempotency_key,
            )
        raise ValueError(f"worker cannot submit {event.kind.value}")

    def _assigned_task(self, event: WorkerEvent) -> TeamTask:
        task = self.repository.get_team_task(event.run_id, event.task_id)
        if task.assignee_agent_id != event.actor_agent_id:
            raise ValueError("only the assigned worker can report this task")
        if task.attempt != event.attempt:
            raise ValueError("worker report attempt does not match the active task")
        if task.state is not TeamTaskState.WORKING and not self.repository.has_event(event.event_id):
            raise ValueError("worker reports require a working task")
        return task

    def _reviewing_task(self, event: WorkerEvent) -> TeamTask:
        task = self.repository.get_team_task(event.run_id, event.task_id)
        snapshot = self.repository.get_team_run(event.run_id)
        reviewer = next((agent for agent in snapshot.agents if agent.agent_id == event.actor_agent_id), None)
        if reviewer is None or reviewer.role is not AgentRole.REVIEWER or task.assignee_agent_id == reviewer.agent_id:
            raise ValueError("only an independent reviewer can report a verdict")
        if task.attempt != event.attempt:
            raise ValueError("review report attempt does not match the active task")
        if task.state is not TeamTaskState.REVIEW and not self.repository.has_event(event.event_id):
            raise ValueError("review reports require a task awaiting review")
        return task

    def _append(self, event: WorkerEvent) -> bool:
        return self.repository.append_event(
            RunEvent(
                event_id=event.event_id,
                event_type=f"worker.{event.kind.value}",
                aggregate_type="team_task",
                aggregate_id=event.task_id,
                payload=event.to_wire(),
                occurred_at=event.occurred_at,
            )
        )


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def artifact_from_argument(value: str, metadata: dict[str, object]) -> ArtifactReference:
    kind, separator, uri = value.partition("=")
    if not separator:
        raise ValueError("artifact must use KIND=URI")
    return ArtifactReference(kind=kind, uri=uri, metadata=metadata)
