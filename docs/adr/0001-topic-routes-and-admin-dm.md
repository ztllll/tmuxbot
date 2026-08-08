# ADR-0001: Route topics to tmux panes and manage them through a privileged DM

- Status: Accepted
- Date: 2026-08-01
- Owners: tmuxbot maintainers

## Context

v0.3 binds one IM source to one tmux session and assembles most frontends around one fixed backend. Telegram already exposes forum topic IDs, while Feishu events expose string thread IDs. Operators need one project group containing multiple independently routed topics, Pi as another interactive CLI, and a DM-accessible management TUI. tmux must remain the shared source of truth so SSH attach and mobile IM operate the same process.

## Decision

Use an exact route key `(channel, credential, chat_id, thread_id)` and map it one-to-one to a concrete tmux pane plus cwd and adapter. Widen `thread_id` to `int | str | None`. Resolve the backend after route lookup through a frontend-owned adapter registry; a bot credential may therefore serve Claude Code, Codex, and Pi routes.

Keep human-readable YAML and the existing `Binding` shape as the compatibility surface. Add atomic route commands and safe reload rather than moving routes into an opaque application database.

Add an explicitly configured Admin DM route. It runs a configurable interactive CLI in tmux, defaults its cwd to the runtime user's home, and receives full Unix-user capability only after strict Boss DM ACL checks.

Implement Pi as a tmux TUI/transcript adapter, not through headless, RPC, SDK, or print modes.

## Alternatives considered

### One bot token per CLI

Rejected because topics are terminal entry points, not provider silos. It prevents one project group from choosing the appropriate TUI per topic and duplicates channel credentials.

### Bind topics directly to provider sessions

Rejected because the durable user-facing object is the tmux pane. Provider sessions can rotate on `/new`, while SSH attach must continue to target the same pane.

### Store routes only in the control-plane database

Rejected because direct operator and Admin-AI editing is a required recovery path. YAML remains reviewable, portable, and easy to repair offline.

### Restrict Admin AI to route subcommands

Rejected because the management session is intentionally the operator's remote terminal. Route commands improve reliability but are not a capability sandbox.

### Use provider SDK/RPC/headless modes

Rejected because they create a second execution surface and break the core property that IM and SSH share the exact interactive TUI.

## Consequences

- Frontend code must stop assuming `frontend.backend` is authoritative.
- Heartbeat, tailing, commands, attachments, status footers, provisioning, and lifecycle must resolve the adapter per binding.
- Route validation must include string Feishu thread IDs and no longer enforce token-to-backend mappings.
- Admin ACL failures must be silent and must occur before reactions, typing, route lookup side effects, or tmux operations.
- Reload needs last-known-good semantics.
- Pi transcript discovery and event parsing become a maintained provider contract.
- Existing bindings remain valid throughout migration.
