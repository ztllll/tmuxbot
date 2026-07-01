# Telegram Message Polish Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Improve Telegram final reply delivery while preserving tmux CLI sessions and existing tool/status aggregation.

**Architecture:** Add a focused renderer for assistant replies, route final replies through an optional Telegram-specific method, and keep `send_html` as the fallback for other frontends. Telegram handles long replies, full-output attachments, and action buttons.

**Tech Stack:** Python 3, aiogram, pytest, existing tmuxbot frontends/backends.

## Global Constraints

- Do not introduce SDK/API model backends.
- Do not implement Telegram draft streaming in this phase.
- Preserve current tool aggregation and plan editing behavior.
- Keep Feishu behavior on the existing fallback path.

---

### Task 1: Final Reply Routing

**Files:**
- Modify: `tests/test_outbound_attachments.py`
- Modify: `tmuxbot/jsonl.py`

**Interfaces:**
- Consumes: frontend optional method `send_assistant_reply(binding, html_text, attachments=None)`.
- Produces: final assistant replies call the optional method when present.

- [ ] Write failing tests for enhanced frontend routing and fallback behavior.
- [ ] Run the targeted tests and confirm failure.
- [ ] Implement optional routing in `jsonl.py`.
- [ ] Run targeted tests and confirm pass.

### Task 2: Telegram Assistant Reply Renderer

**Files:**
- Create: `tmuxbot/replies.py`
- Modify: `tests/test_telegram_replies.py`
- Modify: `tmuxbot/frontends/telegram.py`

**Interfaces:**
- Produces: `render_assistant_reply(binding, html_text, full_output_threshold)` and `TelegramFrontend.send_assistant_reply`.

- [ ] Write failing tests for short reply formatting and long reply attachment behavior.
- [ ] Run tests and confirm failure.
- [ ] Implement renderer and Telegram method.
- [ ] Run tests and confirm pass.

### Task 3: Status Actions

**Files:**
- Modify: `tmuxbot/frontends/telegram.py`
- Modify: `tests/test_telegram_replies.py`

**Interfaces:**
- Uses existing callback route `tui:<token>:refresh` and existing commands `/screen`, `/cc`.

- [ ] Write failing tests for final reply inline keyboard actions.
- [ ] Run tests and confirm failure.
- [ ] Add `screen`, `status`, and `stop` buttons to final replies.
- [ ] Run tests and confirm pass.

### Task 4: Verification

**Files:**
- No production files.

- [ ] Run `uv run pytest -q`.
- [ ] Run `uv run ruff check .`.
- [ ] Run `uv run python -m compileall -q tmuxbot tests`.
- [ ] Restart `tmuxbot.service`.
- [ ] Check logs for startup errors.
