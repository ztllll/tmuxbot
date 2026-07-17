"""``tmuxbot worker`` command-line entrypoints used by managed CLI workers."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from tmuxbot.control_plane.repository import ControlPlaneRepository
from tmuxbot.paths import RuntimePaths
from tmuxbot.teamrun.protocol import WorkerEvent, WorkerEventKind
from tmuxbot.teamrun.scheduler import TeamRunScheduler
from tmuxbot.teamrun.tmux_sender import TmuxManagedSender
from tmuxbot.teamrun.worker import WorkerReporter, artifact_from_argument, now_utc


def add_worker_parser(subparsers: argparse._SubParsersAction) -> None:
    worker = subparsers.add_parser("worker", help="submit a structured TeamRun worker report")
    worker.add_argument("--database", type=Path, help="control-plane SQLite path")
    worker.add_argument("--run", required=True, dest="run_id")
    worker.add_argument("--task", required=True, dest="task_id")
    worker.add_argument("--agent", required=True, dest="agent_id")
    worker.add_argument("--attempt", required=True, type=int)
    worker.add_argument("--idempotency-key", required=True)
    commands = worker.add_subparsers(dest="worker_command", required=True)
    commands.add_parser("claim", help="acknowledge an assigned task")
    progress = commands.add_parser("progress", help="report task progress")
    progress.add_argument("--percent", required=True, type=int)
    artifact = commands.add_parser("publish-artifact", help="register one evidence artifact")
    artifact.add_argument("--artifact", required=True, help="KIND=URI")
    artifact.add_argument("--metadata", default="{}", help="JSON object")
    complete = commands.add_parser("complete", help="submit evidence and request review")
    complete.add_argument("--artifact", action="append", required=True, help="KIND=URI")
    complete.add_argument("--metadata", default="{}", help="JSON object applied to every artifact")
    blocked = commands.add_parser("block", help="report a task blocker")
    blocked.add_argument("--reason", required=True)


def run_worker(args: argparse.Namespace) -> None:
    paths = RuntimePaths.discover(
        os.environ, legacy_project_dir=Path(__file__).resolve().parents[2]
    )
    database_path = args.database or paths.database_file
    repository = ControlPlaneRepository(database_path)
    repository.migrate()
    reporter = WorkerReporter(
        repository,
        TeamRunScheduler(repository, TmuxManagedSender(repository)),
    )
    event = _event_from_args(args)
    task = reporter.report(event)
    print(
        json.dumps(
            {
                "event_id": event.event_id,
                "kind": event.kind.value,
                "run_id": event.run_id,
                "task_id": event.task_id,
                "state": task.state.value if task is not None else None,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )


def _event_from_args(args: argparse.Namespace) -> WorkerEvent:
    kind = WorkerEventKind(
        {
            "claim": WorkerEventKind.TASK_CLAIMED.value,
            "progress": WorkerEventKind.TASK_PROGRESS.value,
            "publish-artifact": WorkerEventKind.ARTIFACT_PUBLISHED.value,
            "complete": WorkerEventKind.TASK_COMPLETED.value,
            "block": WorkerEventKind.TASK_BLOCKED.value,
        }[args.worker_command]
    )
    metadata = _metadata(args.metadata) if hasattr(args, "metadata") else {}
    raw_artifacts: list[str]
    if args.worker_command == "complete":
        raw_artifacts = args.artifact
    elif args.worker_command == "publish-artifact":
        raw_artifacts = [args.artifact]
    else:
        raw_artifacts = []
    return WorkerEvent(
        event_id=(
            f"worker:{args.run_id}:{args.task_id}:{args.attempt}:"
            f"{args.worker_command}:{args.idempotency_key}"
        ),
        kind=kind,
        run_id=args.run_id,
        task_id=args.task_id,
        attempt=args.attempt,
        actor_agent_id=args.agent_id,
        idempotency_key=args.idempotency_key,
        occurred_at=now_utc(),
        evidence=tuple(artifact_from_argument(item, metadata) for item in raw_artifacts),
        message=args.reason if args.worker_command == "block" else None,
        progress_percent=args.percent if args.worker_command == "progress" else None,
    )


def _metadata(raw: str) -> dict[str, object]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("metadata must be a JSON object") from exc
    if not isinstance(value, dict):
        raise ValueError("metadata must be a JSON object")
    return value
