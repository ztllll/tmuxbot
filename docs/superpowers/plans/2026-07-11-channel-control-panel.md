# Channel Control Panel Implementation Plan / 通道轻量控制面板实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a Chinese `/panel` for Telegram and Feishu with per-binding mention policy, common CLI commands, and provider-native model switching.

**Architecture:** Introduce a channel-neutral panel policy/action module and persist `mention_required` on `Binding`. Telegram renders Inline Keyboard, Feishu renders Card JSON 2.0; both route allowlisted actions through the existing dispatcher and tmux-backed native `/model` picker.

**Tech Stack:** Python 3.10+, aiogram, lark-oapi, Feishu Card JSON 2.0, tmux, pytest, Ruff.

## Global Constraints

- Ordinary assistant replies remain button-free.
- Tmux remains the execution plane for Claude and Codex.
- Model names are not hardcoded in panel actions.
- All panel explanations are Chinese.
- Mention policy is binding-scoped and private chats remain mention-free.
- Multi-LLM coordination research produces documentation only; no coordination runtime code is added.

---

### Task 1: Binding-scoped panel policy

**Files:**
- Create: `tmuxbot/control_panel.py`
- Modify: `tmuxbot/state.py`
- Modify: `tmuxbot/config.py`
- Test: `tests/test_control_panel.py`

**Interfaces:**
- `effective_mention_required(binding, frontend_default) -> bool`
- `parse_mention_command(text) -> bool | None | str`
- `save_binding_mention_policy(path, binding, value) -> None`
- `render_panel_text(binding, frontend_default, runtime_mode) -> str`

- [x] Write failing pure tests for policy inheritance, Chinese panel text, command parsing, and YAML persistence.
- [x] Run `.venv/bin/pytest tests/test_control_panel.py -q` and confirm RED.
- [x] Implement the minimal policy and persistence module.
- [x] Run the targeted tests and confirm GREEN.
- [x] Commit with `git commit -m "feat: add binding control panel policy"`.

### Task 2: Telegram `/panel`

**Files:**
- Modify: `tmuxbot/frontends/telegram.py`
- Modify: `tmuxbot/backends/claude_code.py`
- Modify: `tmuxbot/backends/codex.py`
- Test: `tests/test_telegram_panel.py`
- Test: `tests/test_telegram_mentions.py`

**Interfaces:**
- `/panel`, `/settings`, `/mention on|off|default|status`
- Callback prefix: `panel:<binding-token>:<action>`
- Allowlisted command actions dispatch `/status`, `/screen`, `/new`, `/compact`, `/resume`, `/model`, `/esc`, `/cc`.

- [x] Write failing tests for Chinese keyboard structure, mention bypass, callback ACL, persistence, `/new` confirmation, and `/model` dispatch.
- [x] Run targeted tests and confirm RED.
- [x] Implement Telegram handlers and panel callbacks.
- [x] Add `panel`, `settings`, and `mention` to both provider BotCommand menus.
- [x] Run targeted tests and confirm GREEN.
- [x] Commit with `git commit -m "feat: add Telegram control panel"`.

### Task 3: Feishu `/panel` and picker controls

**Files:**
- Modify: `tmuxbot/frontends/feishu_cards.py`
- Modify: `tmuxbot/frontends/feishu.py`
- Test: `tests/test_feishu_panel.py`
- Test: `tests/test_feishu_actions.py`
- Test: `tests/test_feishu_mentions.py`

**Interfaces:**
- `build_feishu_control_panel(...) -> dict[str, Any]`
- Feishu panel callbacks use the same action names as Telegram.
- `send_interaction_card()` emits explicit TUI navigation controls only for requested interaction cards.

- [x] Write failing tests for Chinese Card JSON 2.0 content, buttons, mention bypass, callback ACL, panel refresh, and native `/model` dispatch.
- [x] Run targeted tests and confirm RED.
- [x] Implement Feishu panel rendering, callbacks, and TUI interaction controls.
- [x] Run targeted tests and confirm GREEN.
- [x] Commit with `git commit -m "feat: add Feishu control panel"`.

### Task 4: Research record, documentation, rollout

**Files:**
- Create: `docs/research/2026-07-11-multi-llm-coordination-landscape.md`
- Create: `docs/research/2026-07-11-multi-llm-coordination-landscape-zh.md`
- Modify: `README.md`
- Modify: rollout verification documentation.

- [x] Record expanded multi-agent research without implementing orchestration code.
- [x] Document `/panel`, `/mention`, common actions, and model-picker semantics in Chinese.
- [ ] Run `.venv/bin/ruff check tmuxbot tests` and `.venv/bin/pytest -q`.
- [ ] Deploy locally and to hbhy while preserving tmux sessions.
- [ ] Perform Telegram and Feishu live acceptance, commit, push, and synchronize deployments.
