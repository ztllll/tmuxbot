# ADR-0001: Route topics to tmux panes and manage them through a privileged DM

- Status: Accepted
- Date: 2026-08-01
- Amended: 2026-08-14 (OMP native adapter cutover)
- Owners: tmuxbot maintainers

## Context

An IM credential may serve many project topics and more than one interactive CLI. Telegram exposes integer forum topic IDs, while Feishu exposes string thread IDs. Operators need one project group containing independently routed topics, Oh My Pi as a managed interactive CLI, and a DM-accessible management TUI. tmux must remain the shared source of truth so SSH attach and mobile IM operate the same process.

OMP 17.3.2 differs materially from the retired runtime assumptions: it launches as `omp`, and tmuxbot resumes it only through `--resume <exact-absolute-jsonl-path>`. It writes JSONL v3 under `~/.omp/agent/sessions`, supports busy steering for ordinary prompts, and owns native menus/confirmations that cannot safely be remote-controlled from IM. Its footer is screen presentation, not durable identity.

## Decision

Use an exact route key `(channel, credential, chat_id, thread_id)` and map it one-to-one to a concrete tmux pane plus cwd and adapter. Widen `thread_id` to `int | str | None`. Resolve the backend after route lookup; a bot credential may therefore serve Claude Code, Codex, and OMP routes. The current OMP backend key is exactly `omp`, implemented by `OmpBackend`.

Keep human-readable YAML and the existing `Binding` shape as the route surface. Add atomic route commands and safe reload rather than moving routes into an opaque application database. The Admin DM remains an explicitly configured, strictly ACL-protected route to a management pane.

Keep OMP in its real tmux TUI. Provider discovery and launch policy live in the server-owned registry: `OMP_BIN` resolves the executable, the registry supplies `--approval-mode yolo --extension <managed-extension-absolute-path>`, and resume appends only `--resume <exact-absolute-jsonl-path>`. Web/API clients select a provider ID; they never supply executable paths, tmux targets, or argv.

Treat provider-authored identity and health sidecars as the exact session seam. Handoff records are target-scoped and match `tmuxTarget`, canonical `cwd`, `sessionId`, `transcriptPath`, and live pane `processId`. A brand-new OMP session may publish its path before creating the JSONL; only an official sessions-root path with a matching session ID and process in the exact pane is accepted during that interval, after which transcript header validation is mandatory. A live OMP process without a valid sidecar fails closed. Explicit restart uses clean pane respawn and exact-path resume rather than injecting exit keys.

Treat OMP JSONL v3 as the structured event authority. Reconstruct the current branch through `id/parentId`; read model/thinking/title/usage metadata from canonical change/message rows; parse todo snapshots from `toolResult.details.phases` or newer `user_todo_edit.data.phases`; and recognize plans only from `mode_change.mode="plan"` plus non-empty writes to `local://*-plan.md`. Canonical `type="compaction"` owns compaction completion.

Treat the OMP 17.3.2 native footer/loader as a versioned weak signal. Ordinary prompts and attachments may use OMP's busy steering queue, but control commands require idle. Native menus, approvals, confirmations, and pickers are SSH-only from IM. Bot `/plan` is local status/help and is never injected into the pane.

## Alternatives considered

### One bot token per CLI

Rejected because topics are terminal entry points, not provider silos. It prevents one project group from choosing the appropriate TUI per topic and duplicates channel credentials.

### Bind topics directly to provider sessions

Rejected because the durable user-facing object is the tmux pane. Provider sessions can rotate on `/new`, `/fork`, or `/resume`, while SSH attach must continue to target the same pane. Exact sidecars let the provider publish the new identity without cwd/mtime guessing.

### Store routes only in the control-plane database

Rejected because direct operator and Admin-AI editing is a required recovery path. YAML remains reviewable, portable, and easy to repair offline.

### Restrict Admin AI to route subcommands

Rejected because the management session is intentionally the operator's remote terminal. Route commands improve reliability but are not a capability sandbox.

### Use provider SDK/RPC/headless modes

Rejected because they create a second execution surface and break the core property that IM and SSH share the exact interactive TUI.

### Infer OMP identity from cwd, newest JSONL, or footer text

Rejected because multiple panes can share nearby session trees and screen text changes across versions. Only the exact provider-authored sidecar or an explicitly persisted, header-validated transcript path may identify a route session.

## Consequences

- Frontend code must not assume `frontend.backend` or bot token identity determines the provider.
- Heartbeat, tailing, commands, attachments, status footers, provisioning, and lifecycle resolve the adapter per binding.
- Route validation includes string Feishu thread IDs and accepts only current backend keys.
- Admin ACL failures remain silent and occur before reactions, typing, route lookup side effects, or tmux operations.
- Reload retains last-known-good semantics.
- OMP JSONL v3, exact sidecars, registry launch policy, busy/idle command separation, and SSH-only interactions are maintained provider contracts.
- Existing Claude/Codex behavior remains provider-specific and unchanged by the OMP adapter.
