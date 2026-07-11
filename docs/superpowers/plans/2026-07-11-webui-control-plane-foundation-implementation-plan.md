# WebUI Control-Plane Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` task-by-task. Track all steps with checkboxes.

**Goal:** Deliver an independently runnable, secure, persistent Web foundation with domain contracts, SQLite, append-only events, single-user authentication, and read-only tmux inventory.

**Architecture:** `tmuxbot/control_plane/` owns channel-neutral models, migrations, repository, events, and tmux inventory. `tmuxbot/web/` owns authentication and FastAPI assembly. `tmuxbot web` runs separately and does not start or import the Telegram/Feishu polling lifecycle.

**Tech Stack:** Python 3.10+, FastAPI, Uvicorn, SQLite, pwdlib/Argon2, itsdangerous, pytest, HTTPX.

## Global Constraints

- tmux is the sole execution plane; every tmux operation in this phase is read-only.
- Bind to `127.0.0.1:8765` by default; remote binding is explicit.
- Except for health, auth status, first setup, and login, every API requires authentication.
- Mutating requests require `X-CSRF-Token`; cookies are HTTP-only and SameSite=Lax.
- Store only Argon2 password hashes and SHA-256 session-token hashes.
- Persist `RunEvent` before projection; `event_id` is globally unique and idempotent.
- Preserve all current channel, binding, tmux-target, and provider-session behavior.

---

## Task Map

The Chinese plan is the authoritative step-by-step execution document and contains complete tests, implementations, commands, and expected results:

1. Add Web dependencies and `WebSettings`.
2. Add immutable control-plane domain contracts.
3. Add numbered SQLite migrations and `ControlPlaneRepository`.
4. Add Argon2 single-user authentication, hashed sessions, and CSRF tokens.
5. Add read-only tmux inventory and managed/orphan/ignored classification.
6. Add authenticated FastAPI health, auth, events, and inventory endpoints with Origin checks.
7. Add the independent `tmuxbot web` entry point, systemd unit, environment example, and documentation.

Execute the exact task steps in `2026-07-11-webui-control-plane-foundation-implementation-plan-zh.md`. Required final verification is `make check`, followed by `git diff --check`, a stage commit, and `git push`.
