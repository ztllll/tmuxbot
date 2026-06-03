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
- Add graceful cancellation for tailers and heartbeat loops.
- Make lock acquisition and stale process diagnostics clearer.
- Add structured startup summary and validation output.
- Add a dry-run command that validates config without starting frontends.

Exit criteria:

- `tmuxbot doctor` or equivalent can validate deployment prerequisites.
- Shutdown does not depend on process kill for normal operation.

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

## Near-Term Backlog

1. Replace historical `idle_kill_seconds` examples.
2. Add config validation tests.
3. Add parser fixtures and tests.
4. Split `TelegramFrontend` into transport, ACL/source, commands, and callbacks.
5. Split `FeishuFrontend` REST card operations from message dispatch.
6. Add a non-network `tmuxbot validate-config` path.
7. Reword README policy section to separate official facts from project design.
