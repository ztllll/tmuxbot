from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

from tmuxbot.control_plane.repository import ControlPlaneRepository
from tmuxbot.teamrun.domain import (
    AgentRole,
    TeamAgent,
    TeamRun,
    TeamRunSnapshot,
    TeamRunState,
    TeamTask,
    TeamTaskState,
)
from tmuxbot.teamrun.protocol import ArtifactReference, ReviewRequest


log = logging.getLogger(__name__)


class TmuxTaskSender(Protocol):
    def is_registered(self, managed_session_id: str) -> bool: ...

    def send(self, managed_session_id: str, envelope: dict[str, Any]) -> None: ...


@dataclass(frozen=True, slots=True)
class ArtifactInput:
    kind: str
    uri: str
    metadata: Mapping[str, object]


class TeamRunScheduler:
    def __init__(
        self,
        repository: ControlPlaneRepository,
        sender: TmuxTaskSender,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.repository = repository
        self.sender = sender
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def create_deterministic_run(
        self,
        *,
        run_id: str,
        goal: str,
        agents: Mapping[AgentRole, str],
        tasks: list[Mapping[str, object]],
        idempotency_key: str,
    ) -> TeamRunSnapshot:
        required_roles = {AgentRole.COORDINATOR, AgentRole.IMPLEMENTER, AgentRole.REVIEWER}
        if set(agents) != required_roles:
            raise ValueError("deterministic run requires coordinator, implementer, and reviewer")
        if any(not self.sender.is_registered(session_id) for session_id in agents.values()):
            raise ValueError("every agent must reference a registered managed session")
        now = self.clock()
        run = TeamRun(run_id, goal, TeamRunState.DRAFT, 1, now, now)
        agent_records = [
            TeamAgent(f"{run_id}:{role.value}", run_id, role, agents[role])
            for role in (AgentRole.COORDINATOR, AgentRole.IMPLEMENTER, AgentRole.REVIEWER)
        ]
        for item in tasks:
            role = AgentRole(str(item.get("role", AgentRole.IMPLEMENTER.value)))
            requires_write = bool(item.get("requires_write", False))
            if role is AgentRole.REVIEWER or (
                requires_write and role is not AgentRole.IMPLEMENTER
            ):
                raise ValueError("task role capability is not supported by deterministic V1")
        task_records = [
            TeamTask(
                task_id=str(item["task_id"]),
                run_id=run_id,
                title=str(item["title"]),
                goal=str(item["goal"]),
                role=AgentRole(str(item.get("role", AgentRole.IMPLEMENTER.value))),
                state=TeamTaskState.PENDING,
                dependencies=tuple(str(value) for value in item.get("dependencies", [])),
                requires_write=bool(item.get("requires_write", False)),
                max_attempts=int(item.get("max_attempts", 2)),
                attempt=0,
                assignee_agent_id=None,
                created_at=now,
                updated_at=now,
            )
            for item in tasks
        ]
        self.repository.create_team_run(
            run,
            agent_records,
            task_records,
            event_id=f"teamrun:{run_id}:create:{idempotency_key}",
        )
        log.info("teamrun created run=%s tasks=%d", run_id, len(task_records))
        return self.repository.get_team_run(run_id)

    def start(self, run_id: str, *, idempotency_key: str) -> TeamRunSnapshot:
        self.repository.set_team_run_state(
            run_id,
            allowed={TeamRunState.DRAFT, TeamRunState.PAUSED},
            state=TeamRunState.RUNNING,
            event_id=f"teamrun:{run_id}:start:{idempotency_key}",
            now=self.clock(),
        )
        self._dispatch_ready(run_id)
        log.info("teamrun started run=%s", run_id)
        return self.repository.get_team_run(run_id)

    def pause(self, run_id: str, *, idempotency_key: str) -> TeamRunSnapshot:
        self.repository.set_team_run_state(
            run_id,
            allowed={TeamRunState.DRAFT, TeamRunState.RUNNING},
            state=TeamRunState.PAUSED,
            event_id=f"teamrun:{run_id}:pause:{idempotency_key}",
            now=self.clock(),
        )
        return self.repository.get_team_run(run_id)

    def resume(self, run_id: str, *, idempotency_key: str) -> TeamRunSnapshot:
        self.repository.set_team_run_state(
            run_id,
            allowed={TeamRunState.PAUSED},
            state=TeamRunState.RUNNING,
            event_id=f"teamrun:{run_id}:resume:{idempotency_key}",
            now=self.clock(),
        )
        self._dispatch_ready(run_id)
        return self.repository.get_team_run(run_id)

    def stop(
        self, run_id: str, *, reason: str, idempotency_key: str
    ) -> TeamRunSnapshot:
        self.repository.stop_team_run(
            run_id,
            reason=reason,
            event_id=f"teamrun:{run_id}:stop:{idempotency_key}",
            now=self.clock(),
        )
        return self.repository.get_team_run(run_id)

    def complete_task(
        self,
        run_id: str,
        task_id: str,
        *,
        agent_id: str,
        artifacts: list[ArtifactInput],
        idempotency_key: str,
    ) -> TeamTask:
        task = self.repository.complete_team_task(
            run_id,
            task_id,
            agent_id=agent_id,
            artifacts=[(item.kind, item.uri, dict(item.metadata)) for item in artifacts],
            idempotency_key=idempotency_key,
            now=self.clock(),
        )
        snapshot = self.repository.get_team_run(run_id)
        reviewer = next(agent for agent in snapshot.agents if agent.role is AgentRole.REVIEWER)
        review_key = f"teamrun:{run_id}:review:{task_id}:{task.attempt}"
        self.sender.send(
            reviewer.managed_session_id,
            ReviewRequest(
                message_id=review_key,
                run_id=run_id,
                task_id=task_id,
                attempt=task.attempt,
                reviewer_agent_id=reviewer.agent_id,
                producer_agent_id=agent_id,
                goal=task.goal,
                artifacts=tuple(
                    ArtifactReference(item.kind, item.uri, item.metadata) for item in artifacts
                ),
                idempotency_key=review_key,
            ).to_wire(),
        )
        return task

    def review_task(
        self,
        run_id: str,
        task_id: str,
        *,
        reviewer_agent_id: str,
        verdict: str,
        notes: str,
        idempotency_key: str,
    ) -> TeamTask:
        task = self.repository.review_team_task(
            run_id,
            task_id,
            reviewer_agent_id=reviewer_agent_id,
            verdict=verdict,
            notes=notes,
            idempotency_key=idempotency_key,
            now=self.clock(),
        )
        if task.state is TeamTaskState.ACCEPTED:
            self.repository.refresh_task_readiness(run_id, now=self.clock())
            self._dispatch_ready(run_id)
            self.repository.complete_run_if_accepted(run_id, now=self.clock())
        elif task.state is TeamTaskState.RETRYING:
            self.repository.refresh_task_readiness(run_id, now=self.clock())
            self._dispatch_ready(run_id)
            task = self.repository.get_team_task(run_id, task_id)
        return task

    def block_task(
        self,
        run_id: str,
        task_id: str,
        *,
        agent_id: str,
        reason: str,
        idempotency_key: str,
    ) -> TeamTask:
        return self.repository.block_team_task(
            run_id,
            task_id,
            agent_id=agent_id,
            reason=reason,
            idempotency_key=idempotency_key,
            now=self.clock(),
        )

    def reconcile(self) -> list[str]:
        for run in self.repository.list_team_runs():
            if run.state is not TeamRunState.RUNNING:
                continue
            self._recover_written_dispatches(run.run_id)
            self._deliver_pending_dispatches(run.run_id)
        operator_runs = self.repository.reconcile_team_runs(now=self.clock())
        for run in self.repository.list_team_runs():
            if run.state is not TeamRunState.RUNNING:
                continue
            self.repository.refresh_task_readiness(run.run_id, now=self.clock())
            self._dispatch_ready(run.run_id)
        return operator_runs

    def _dispatch_ready(self, run_id: str) -> None:
        self.repository.refresh_task_readiness(run_id, now=self.clock())
        for task in self.repository.list_ready_tasks(run_id):
            claimed = self.repository.claim_team_task(
                run_id,
                task.task_id,
                event_id=f"teamrun:{run_id}:dispatch:{task.task_id}:{task.attempt + 1}",
                now=self.clock(),
            )
            if claimed is None:
                continue
            _assigned, _agent = claimed
        self._deliver_pending_dispatches(run_id)

    def _deliver_pending_dispatches(self, run_id: str) -> None:
        for command in self.repository.list_dispatch_commands(run_id, states={"pending"}):
            try:
                log.info(
                    "teamrun dispatch sending command=%s run=%s task=%s attempt=%d",
                    command.command_id, command.run_id, command.task_id, command.attempt,
                )
                self.sender.send(command.managed_session_id, dict(command.envelope))
            except Exception as exc:
                self.repository.mark_dispatch_uncertain(
                    command.command_id, error=str(exc), now=self.clock()
                )
                log.warning(
                    "teamrun dispatch uncertain command=%s run=%s task=%s error=%s",
                    command.command_id, command.run_id, command.task_id, type(exc).__name__,
                )
                continue
            self.repository.mark_dispatch_tmux_written(command.command_id, now=self.clock())
            self.repository.mark_mailbox_delivered(
                run_id,
                command.command_id,
                now=self.clock(),
            )
            self.repository.mark_team_task_working(
                run_id,
                command.task_id,
                event_id=f"teamrun:{run_id}:working:{command.task_id}:{command.attempt}",
                now=self.clock(),
            )
            log.info(
                "teamrun dispatch tmux_written command=%s run=%s task=%s attempt=%d",
                command.command_id, command.run_id, command.task_id, command.attempt,
            )

    def _recover_written_dispatches(self, run_id: str) -> None:
        for command in self.repository.list_dispatch_commands(run_id, states={"tmux_written"}):
            self.repository.mark_mailbox_delivered(run_id, command.command_id, now=self.clock())
            self.repository.mark_team_task_working(
                run_id,
                command.task_id,
                event_id=f"teamrun:{run_id}:working:{command.task_id}:{command.attempt}",
                now=self.clock(),
            )
            log.info(
                "teamrun dispatch recovered command=%s run=%s task=%s",
                command.command_id, command.run_id, command.task_id,
            )
