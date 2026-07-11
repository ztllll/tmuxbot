# Telegram State Badges Implementation Plan / Telegram 状态标识实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Render provider-neutral runtime state as a compact Telegram Emoji badge and document why Feishu alone has native colored card headers.

**Architecture:** Reuse `ReplyDocument.state`, which already receives explicit `display_state` metadata or normalized terminal status. Add a pure Telegram state-to-label mapping inside the channel-neutral renderer; leave missing state unrendered and keep all new replies button-free.

**Tech Stack:** Python 3.10+, aiogram HTML messages, pytest, Ruff.

## Global Constraints

- Tmux remains the execution plane.
- Telegram messages do not regain persistent buttons.
- Feishu state-color behavior remains unchanged.
- Missing state adds no Telegram status line.
- User-facing documentation is available in Chinese; English remains available for agent implementation context.

---

### Task 1: Telegram state badge renderer / Telegram 状态标识渲染

**Files:**
- Modify: `tmuxbot/core/rich_messages.py`
- Test: `tests/test_rich_messages.py`

**Interfaces:**
- Consumes: `ReplyDocument.state: str | None`
- Produces: `telegram_state_badge(state: str | None) -> str | None`

- [x] **Step 1: Write a parameterized failing test** covering working, waiting, completed, idle, error, blocked, dead, info, unknown, and `None`.
- [x] **Step 2: Run** `.venv/bin/pytest tests/test_rich_messages.py -q` and confirm the badge assertions fail because no status line exists.
- [x] **Step 3: Implement the minimal state mapping** and insert its result between the Telegram reply header and body.
- [x] **Step 4: Run** `.venv/bin/pytest tests/test_rich_messages.py tests/test_telegram_replies.py -q` and confirm all tests pass.
- [x] **Step 5: Commit** with `git commit -m "feat: show runtime state in Telegram replies"`.

### Task 2: Platform capability documentation / 平台能力文档

**Files:**
- Modify: `README.md`
- Modify: `docs/superpowers/specs/2026-07-11-button-free-state-colors-design-zh.md`

**Interfaces:**
- Documents the same mapping implemented by `telegram_state_badge()`.

- [x] **Step 1: Update README** with a concise Feishu-native versus Telegram-text comparison.
- [x] **Step 2: Update the Chinese design guide** with the Telegram mapping table and Bot API limitation.
- [x] **Step 3: Run** `.venv/bin/ruff check tmuxbot tests` and `.venv/bin/pytest -q`.
- [ ] **Step 4: Commit and push** the documentation, then deploy without changing tmux session counts.
