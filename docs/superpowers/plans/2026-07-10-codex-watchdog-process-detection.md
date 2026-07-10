# Codex Watchdog Process Detection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop the lifecycle watchdog from injecting the Codex startup command into a healthy standalone Codex TUI.

**Architecture:** Centralize Codex and shell pane-command classification in `CodexBackend`. Reuse the Codex predicate for the initial health check and readiness polling, and only start Codex from a recognized shell.

**Tech Stack:** Python 3.10+, asyncio, pytest, tmux

## Global Constraints

- Preserve npm-wrapper Codex support where `pane_current_command` is `node`.
- Support standalone Codex where `pane_current_command` is `codex`.
- Never paste a startup command into an unknown foreground process.
- Preserve unrelated dirty-worktree changes.

---

### Task 1: Add process-detection regression coverage

**Files:**
- Modify: `tests/test_codex_backend.py`

**Interfaces:**
- Consumes: `CodexBackend.ensure_running(binding)`
- Produces: Regression coverage for `node`, `codex`, shell, and unknown pane commands.

- [x] **Step 1: Write failing tests**

Add async test cases that monkeypatch tmux helpers and record calls to `tmux_send_text`.

- [x] **Step 2: Verify the regression test fails**

Run: `pytest tests/test_codex_backend.py -q`

Expected: the standalone `codex` and unknown-command cases fail because the current implementation injects the startup command.

- [x] **Step 3: Implement minimal process classification**

Add Codex command and shell command sets plus small predicate methods. Update `ensure_running` to return for a running Codex process, start only from a shell, warn and return for unknown processes, and reuse the Codex predicate in the readiness loop.

- [x] **Step 4: Verify focused tests pass**

Run: `pytest tests/test_codex_backend.py -q`

Expected: all focused tests pass.

### Task 2: Verify and deploy

**Files:**
- Modify: `tmuxbot/backends/codex.py`
- Test: `tests/test_codex_backend.py`

**Interfaces:**
- Consumes: lifecycle watchdog and tmux pane state.
- Produces: safe process detection in the running tmuxbot service.

- [x] **Step 1: Run the full test suite**

Run: `pytest -q`

Expected: all tests pass.

- [x] **Step 2: Run static checks**

Run: `ruff check tmuxbot/backends/codex.py tests/test_codex_backend.py`

Expected: no lint errors.

- [x] **Step 3: Restart the service**

Run: `systemctl --user restart tmuxbot.service`

Expected: service becomes active and starts one lifecycle watchdog.

- [x] **Step 4: Observe two lifecycle intervals**

Check the service log and `codex-tmuxbot` pane after at least 65 seconds.

Expected: no new `codex --dangerously-bypass-approvals-and-sandbox` prompt and no repeated slow `ensure_running(watchdog)` entries for `top-coordinator-codex`.
