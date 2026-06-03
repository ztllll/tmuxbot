# tmuxbot — Productization Plan

This document is the maintenance track for turning tmuxbot from a proven personal
daemon into a long-lived product-quality codebase. It records the next work in
terms of risk reduction, not feature wishes.

## Product Principle

tmuxbot is a local TUI bridge. Its core promise is controlled remote operation of
interactive AI CLIs through tmux, with IM frontends as transport only. The source
of truth remains the local tmux pane plus the CLI's own session logs.

The product should stay conservative:

- Keep tmux injection and JSONL tailing as the core architecture.
- Treat each frontend/backend pair as an adapter around shared dispatch logic.
- Prefer explicit configuration validation over clever runtime recovery.
- Put parser, formatter, and routing behavior under tests before large rewrites.
- Avoid policy claims that are not directly guaranteed by vendor documentation.

## Current Assessment

Strengths:

- The domain model is real: one IM endpoint maps to one tmux pane and cwd.
- Backend and frontend boundaries already exist.
- Critical operational races are known and handled: idle-before-enter, JSONL
  backlog protection, session switch detection, picker fallback, task ownership.
- Deployment artifacts and operator scripts exist.

Main risks:

- Configuration mistakes can still enter runtime and cause cross-chat or cross-cwd
  confusion.
- Parser behavior is high value but mostly untested.
- `TelegramFrontend` and `FeishuFrontend` still contain mixed concerns: ACL,
  transport, provisioning commands, formatting, and callbacks.
- Runtime state is a global singleton, which makes tests and multi-instance
  reasoning harder.
- Documentation contains some historical residue from the single-file era.

## Maintenance Track

### Phase 0: Guardrails

Goal: make the current architecture safer without changing behavior.

- Add startup validation for `bindings.yaml`.
- Add unit tests for validation and pure helpers.
- Add a committed test runner and quality commands.
- Update public docs so they match the current architecture.
- Keep internal/sensitive files ignored.

Exit criteria:

- `python -m pytest` passes locally.
- Bad duplicate bindings fail before frontends start.
- New contributors can see the supported quality commands in one place.

### Phase 1: Parser Coverage

Goal: lock down the behavior most likely to regress.

- Cover `encode_cwd`, Telegram splitting, HTML escaping boundaries.
- Cover `ClaudeCodeBackend.parse_event` for assistant text, tool use, sidechain
  filtering, compact metadata, and token aggregation.
- Cover `CodexBackend.parse_event` for assistant text, tool calls, and duplicate
  event suppression.
- Store JSONL fixtures in `tests/fixtures/`.

Exit criteria:

- Parser tests cover representative real JSONL shapes.
- A backend parser change fails tests before reaching deployment.

### Phase 1.5: Slash Command And TUI Interaction Adapter

Goal: make Claude Code and Codex slash commands reliable from IM frontends
without reimplementing each upstream TUI command.

- Add a backend command registry that describes each slash command by behavior:
  capture, state transition, workflow, interactive TUI, or blocked/dangerous.
- Add command transactions that record the injected command, starting JSONL
  offset/session, pane snapshot, expected result source, timeout, and current
  interaction state.
- Generalize picker detection into an interaction detector that can surface
  picker/dialog/approval/slider/text-entry screens as frontend cards.
- Add a frontend-neutral interaction card contract for remote keys: Up, Down,
  Left, Right, Tab, Space, Enter, Escape, Refresh, plus optional semantic
  actions for known prompts.
- Treat `/plan` as a first-class workflow: inject the command, let transcript
  output flow normally, detect plan approval, and route approve/reject/modify
  actions back to the active TUI.
- Forward unknown slash commands through the active provider, but attach a
  short failure probe that checks transcript and pane deltas for unknown or
  unsupported command errors.
- Add provider-specific busy behavior, especially Codex's queued slash-command
  path while a task is running.

Exit criteria:

- Existing `/context`, `/status`, `/usage`, `/compact`, `/clear`, `/new`,
  `/resume`, and `/rename` behavior is preserved under the new command engine.
- `/plan`, `/model`, `/permissions`, and `/resume` can be driven from Telegram
  and Feishu without needing direct terminal access.
- Unknown slash commands no longer silently disappear when the provider rejects
  them.
- Interaction state is per binding and is recoverable after card refresh.

### Phase 2: Core Extraction

Goal: shrink frontend files and remove hidden coupling.

- Move binding lookup, ACL decisions, and source resolution into `tmuxbot/core/`.
- Move outbound message chunking/formatting into frontend-specific helpers.
- Move provisioning command handlers behind a small frontend-neutral service.
- Replace global lookups in callbacks with frontend-local binding lookup.

Exit criteria:

- Frontend classes mostly adapt transport events to shared application services.
- No command handler needs to inspect all global bindings unless explicitly
  performing an administrative operation.

### Phase 3: Runtime Lifecycle

Goal: make daemon behavior observable and predictable.

- Track background tasks by type and binding.
- Replace process-name-only `ensure_running` checks with a lifecycle state
  machine: tmux missing, pane shell, CLI booting, CLI ready, CLI busy, TUI
  blocked, exited, and unhealthy.
- Validate resume identifiers before injecting provider resume commands; never
  pass arbitrary strings from runtime state to `--resume`.
- Add provider-specific startup readiness checks before forwarding the user's
  pending message into the pane.
- Queue inbound messages while a backend is booting or recovering, then flush
  only after the backend is confirmed ready.
- Add recovery logging around every lifecycle decision: observed pane command,
  child process, selected resume id, readiness screen, and final outcome.
- Add a periodic health probe for bound panes so exited or wedged CLIs are
  detected before the next user message when possible.
- Add graceful cancellation for tailers and heartbeat loops.
- Make lock acquisition and stale process diagnostics clearer.
- Add structured startup summary and validation output.
- Add a dry-run command that validates config without starting frontends.

Exit criteria:

- `tmuxbot doctor` or equivalent can validate deployment prerequisites.
- Shutdown does not depend on process kill for normal operation.
- A message sent after Claude/Codex exits restarts the provider, resumes the
  most recent valid session when supported, waits for readiness, and then
  injects the original message exactly once.

### Phase 4: Product Surface

Goal: make the project installable and operable.

- Add console entry point.
- Add pinned dev tooling.
- Add release checklist.
- Split README into quickstart plus operator notes.
- Keep policy-sensitive claims factual and sourced.

Exit criteria:

- A fresh machine can install, validate config, and start from documented steps.
- Release notes distinguish behavior changes, operational changes, and docs.

## Architectural Invariants

- One bot token maps to one backend type.
- `(channel, bot_token_env, chat_id, thread_id)` must be unique.
- `tmux_session` must be unique.
- `cwd` should be unique per backend session source.
- A frontend may only route messages to bindings assigned to that frontend.
- Unknown sources are silent unless they are explicit provisioning commands.
- Normal text injection must not send Escape first.
- TUI command parsers belong to backends, not frontends.
- Slash command routing must go through the shared command adapter, not
  frontend-specific command handlers.
- TUI interactions are remote keyboard sessions over the existing tmux pane;
  semantic buttons are optional enhancements, not the source of truth.

## Near-Term Backlog

1. Replace historical `idle_kill_seconds` examples.
2. Add config validation tests.
3. Add parser fixtures and tests.
4. Split `TelegramFrontend` into transport, ACL/source, commands, and callbacks.
5. Split `FeishuFrontend` REST card operations from message dispatch.
6. Add a non-network `tmuxbot validate-config` path.
7. Reword README policy section to separate official facts from project design.
8. Introduce `CommandSpec`/command registry for Claude and Codex built-ins.
9. Add `CommandTransaction` tracking around slash command injection.
10. Replace picker-only fallback with generic TUI interaction detection.
11. Add frontend-neutral interaction cards and callback routing.
12. Add `/plan` approval handling as the first semantic workflow adapter.
13. Add unknown slash-command failure probes from JSONL and pane deltas.
14. Replace `ensure_running` with lifecycle-aware recovery for exited or
    unhealthy Claude/Codex panes.
15. Add resume-id validation and boot readiness checks before message injection.
16. Add queue-and-flush behavior for messages received while a backend is
    recovering.
