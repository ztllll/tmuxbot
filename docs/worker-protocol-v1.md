# Multi-CLI Worker Protocol v1

`tmuxbot.worker.v1` is the internal contract between a TeamRun scheduler and a
CLI running inside a managed tmux pane. It does not replace tmux: tmux remains
the execution and observation substrate. The protocol makes task lifecycle
transitions machine-readable, durable, and visible to people through WebUI,
Telegram, and Feishu projections.

## Boundary

- The scheduler sends a `task.assignment` command with stable `run_id`,
  `task_id`, `attempt`, worker identity, constraints, expected artifacts, and
  acceptance criteria.
- A worker reports one versioned event at a time: `task.claimed`,
  `task.progress`, `artifact.published`, `task.completed`, `task.blocked`,
  `review.requested`, or `review.completed`.
- Every command and report has an idempotency key. A later durable outbox may
  retry delivery without creating a second task transition.
- Completion and artifact publication must contain structured evidence
  (`kind`, `uri`, metadata). A reviewer returns an explicit `approved` or
  `rejected` verdict; prose is explanatory only.
- The scheduler uses a versioned `review.requested` command to hand those
  artifacts to an independent reviewer; a producer cannot review its own work.

## Invariants

1. A task attempt is at least one and belongs to exactly one run and task.
2. A blocked task includes a reason. Progress is an integer from 0 to 100.
3. A completion cannot advance to review without at least one evidence item.
4. Protocol parsing rejects unknown versions and malformed fields before a
   repository transition is attempted.
5. UI and IM channels are projections of these events, never independent
   schedulers. They may display prose but do not infer lifecycle state from it.

## Rollout

P0 publishes the pure, tested contract in `tmuxbot.teamrun.protocol`. P1 adds
the worker-side commands that submit the reports. P2 adapts Claude and Codex
terminal prompts to emit them; P3 persists delivery receipts and recovery;
P4 renders the append-only event timeline in WebUI.

## Worker commands (P1)

Every command requires the run, task, assigned agent, active attempt and a
stable idempotency key. It writes to the local control-plane database selected
by `TMUXBOT_DATABASE` (or `--database`), never to an IM channel.

```bash
tmuxbot worker --run run-42 --task implement --agent run-42:implementer \
  --attempt 1 --idempotency-key claim-1 claim
tmuxbot worker --run run-42 --task implement --agent run-42:implementer \
  --attempt 1 --idempotency-key progress-50 progress --percent 50
tmuxbot worker --run run-42 --task implement --agent run-42:implementer \
  --attempt 1 --idempotency-key test-1 publish-artifact \
  --artifact 'test=pytest://468-passed' --metadata '{"passed":468}'
tmuxbot worker --run run-42 --task implement --agent run-42:implementer \
  --attempt 1 --idempotency-key complete-1 complete \
  --artifact 'test=pytest://468-passed'
tmuxbot worker --run run-42 --task implement --agent run-42:implementer \
  --attempt 1 --idempotency-key blocked-1 block --reason 'credential required'
```

The command verifies that the caller is the active assignee for the active
attempt. A published artifact is deduplicated when the same evidence is later
used in `complete`; completion still moves the task to independent review.
