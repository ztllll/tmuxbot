# WebUI Multi-Agent Control Plane Delivery Roadmap

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` for each phase and track every implementation step with checkboxes.

**Goal:** Deliver a single-user WebUI and deterministic multi-CLI agent control plane without replacing tmux or regressing Telegram/Feishu Runtime V2.

**Architecture:** The WebUI is a separate process sharing tmuxbot contracts, SQLite state, and append-only `RunEvent` records. tmux retains interactive CLI processes and native context; the scheduler owns tasks, leases, mailbox messages, evidence, and acceptance.

**Tech Stack:** Python 3.10+, FastAPI, Uvicorn, SQLite, React, TypeScript, Vite, xterm.js, pytest, Vitest, Playwright.

## Global Constraints

- tmux remains the sole execution plane and survives Web service restarts.
- Bind to `127.0.0.1` by default; remote binding is explicit.
- V1 is single-user and single-host; all terminal and mutation access is authenticated.
- WebUI, Telegram, Feishu, and notifications consume persisted `RunEvent` records only.
- Never overwrite global Codex, Claude, MCP, `CLAUDE.md`, or `AGENTS.md` configuration.
- Hide unsupported provider capabilities; never guess CLI commands.
- Shared-directory mode has one writer; parallel writers require separate worktrees.
- Every phase is independently testable and preserves existing bindings, tmux targets, channels, and provider session identity.

---

## Delivery Phases

1. **Control-plane foundation:** configuration, SQLite migrations, domain contracts, `RunEvent`, single-user authentication, read-only tmux inventory, and orphan classification. Detailed in `2026-07-11-webui-control-plane-foundation-implementation-plan.md`.
2. **Command Center and terminal:** React shell, Run Spine visual system, distinct desktop/mobile compositions, WebSocket events, xterm.js observe mode, and audited takeover mode.
3. **Project launch and Provider Profiles:** safe path validation, allowlisted CLI probes, capability matrix, tmux provisioning/adoption, and native Codex/Claude resume.
4. **Deterministic TeamRun scheduling:** validated DAGs, roles, write leases, mailbox, artifacts, bounded retry, review, and acceptance gates.
5. **Telegram/Feishu projections:** status, approval, pause/resume/stop, and native attachments through the same event projections and scheduler command service.
6. **Recovery, replay, and context governance:** reconciliation, checkpoint artifacts, provider-native compaction, context pressure, and orphan adopt/archive/ignore.
7. **Worktree parallelism:** branches/worktrees, `.worktreeinclude`, bounded hooks, FileClaim preflight, actual-diff validation, merge queue, and owned process/port cleanup.
8. **Dynamic teams and brainstorm mode:** optional UI Designer, hidden first round, objection matrix, team templates, more providers, and advisory routing.

## Release Gates

- [ ] `make check` passes.
- [ ] Phase-specific integration and browser tests pass.
- [ ] Empty-database and existing-database migration tests pass.
- [ ] Security, recovery, and channel compatibility requirements match the approved spec.
- [ ] `git diff --check` passes.
- [ ] The phase lands as an independently revertible commit and is pushed to the active branch.
