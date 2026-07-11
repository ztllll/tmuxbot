# Button-Free State Colors Implementation Plan / 无按钮状态色实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal / 目标:** Remove persistent reply buttons from Telegram and Feishu, and make Feishu card headers communicate runtime state through color.

**Architecture / 架构:** Keep legacy callback handlers for already-sent messages, but make new renderers ignore `ReplyEnvelope.actions`. Derive a display state from explicit metadata or normalized terminal status, then map it to Feishu header templates; streaming cards force working yellow and successful final cards force completed green.

**Tech Stack / 技术栈:** Python 3.10+, aiogram, lark-oapi, Feishu Card JSON 2.0, pytest.

## Global Constraints / 全局约束

- New Telegram and Feishu assistant replies contain no persistent buttons.
- 新 Telegram、飞书回复均不显示常驻按钮，操作统一使用 `/` 命令。
- Legacy callback handlers remain available for old messages.
- Feishu colors: working yellow, waiting orange, completed/idle green, blocked/dead/error red, info blue, unknown grey.
- Feishu Card JSON 2.0 footer uses notation-sized grey text, not deprecated `note`.
- Tmux runtime, attachments, streaming, long-output fallback, and provider adapters remain unchanged.

---

### Task 1: Remove persistent reply controls / 移除常驻回复按钮

**Files:**
- Modify: `tmuxbot/frontends/telegram.py`
- Modify: `tmuxbot/frontends/feishu_cards.py`
- Modify: `tmuxbot/frontends/feishu.py`
- Test: `tests/test_telegram_replies.py`
- Test: `tests/test_feishu_cards.py`
- Test: `tests/test_channel_reply_contract.py`

**Interfaces:**
- New renderers ignore `ReplyEnvelope.actions`.
- `TelegramFrontend.capabilities.supports_actions` and `FeishuFrontend.capabilities.supports_actions` become `False`.

- [x] Write failing tests asserting no Telegram `reply_markup`, no Feishu `button` elements, and both capabilities are false.
- [x] Run targeted tests and verify failures against current button-producing behavior.
- [x] Remove assistant-reply keyboard/card button generation while retaining old callback handlers.
- [x] Run targeted tests and verify pass.
- [x] Commit with `git commit -m "feat: remove persistent reply buttons"`.

### Task 2: Add state-aware Feishu presentation / 增加飞书状态色

**Files:**
- Modify: `tmuxbot/core/rich_messages.py`
- Modify: `tmuxbot/frontends/feishu_cards.py`
- Modify: `tmuxbot/frontends/feishu.py`
- Modify: `tmuxbot/jsonl.py`
- Test: `tests/test_rich_messages.py`
- Test: `tests/test_feishu_cards.py`
- Test: `tests/test_feishu_streaming.py`

**Interfaces:**
- `ReplyEnvelope.metadata["display_state"]` overrides terminal-derived state.
- Card template mapping: `working=yellow`, `waiting=orange`, `completed|idle=green`, `error|blocked|dead=red`, `info=blue`, fallback `grey`.

- [x] Write failing tests for every state color, streaming start yellow, streaming final green, and notation footer structure.
- [x] Run targeted tests and verify the current blue-working/note/grey-final behavior fails.
- [x] Implement display-state override, template mapping, streaming overrides, and `div` notation footer.
- [x] Mark final assistant replies completed unless normalized status is waiting/error; mark informational status cards info.
- [x] Run targeted tests and verify pass.
- [x] Commit with `git commit -m "feat: color Feishu cards by runtime state"`.

### Task 3: Bilingual documentation and rollout / 双语文档与部署

**Files:**
- Create: `docs/superpowers/specs/2026-07-11-button-free-state-colors-design-zh.md`
- Modify: `README.md`
- Modify: `docs/superpowers/verification/2026-07-10-cross-channel-rich-messages-rollout.md`

**Interfaces:**
- English implementation documents remain canonical for agents.
- Chinese companion documents explain user-visible behavior and acceptance results.

- [x] Write the Chinese companion design and update README behavior descriptions.
- [x] Run `ruff check tmuxbot tests` and the full `pytest -q` suite.
- [x] Deploy locally and to both hbhy Feishu services without changing tmux sessions.
- [x] Send Telegram and Feishu acceptance messages and verify no buttons plus correct colors.
- [x] Record rollout evidence, commit, push, and synchronize hbhy.
