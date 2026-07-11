# Cross-Channel Rich Messages Rollout Verification

Date: 2026-07-10 UTC / 2026-07-11 CST

Commit: `f485d9a`

Branch: `productization-prep`

## Automated verification

- Local: `ruff check tmuxbot tests` passed.
- Local: `pytest -q` passed with 147 tests and one upstream lark-oapi deprecation warning.
- hbhy: `ruff check tmuxbot tests` passed.
- hbhy: `pytest -q` passed with 147 tests and the same upstream warning.

## Telegram rollout

- Restarted the local `tmuxbot.service`; the service returned active with both Telegram frontends polling.
- Sent a live rich-reply acceptance message through the Codex Telegram bot.
- Verified native upload of one local document and one local image.
- Verified the body containing a raw `<-` sequence was delivered after unknown-tag sanitization.
- No new Telegram entity parsing errors appeared after the sanitizer-enabled restart.
- Existing tmux targets were not recreated or renamed.

## hbhy Feishu rollout

- Restored key-based SSH access for the existing `hbhy` account and verified `BatchMode=yes` login.
- Fast-forwarded the deployment checkout to `f485d9a` and refreshed the editable `feishu,dev` installation.
- Restarted `tmuxbot.service` and `tmuxbot-codex.service`; both returned active.
- Tmux session count remained 21 before and after service restart.
- Claude service retained 12 Feishu-to-tmux bindings; Codex service retained 9.
- Sent Card JSON 2.0 acceptance replies through both Feishu applications.
- Verified native document and image uploads for both provider paths.
- Acceptance cards included header, summary, provider tag, body components, and screen/status/cancel/interrupt buttons.
- Recent service logs contained no traceback, card-send, attachment-upload, or callback execution errors.

## CardKit streaming rollout

- Claude Feishu application: create-card, component content update, and final close all succeeded.
- Enabled `FEISHU_STREAMING=1` only for the Claude Feishu application.
- Codex Feishu application initially lacked `cardkit:card:write`; after the permission was
  approved and published, create-card, component content update, and final close all succeeded.
- Enabled `FEISHU_CODEX_STREAMING=1` after the live CardKit canary passed.
- Global streaming remains off, while both Feishu applications use explicit per-app streaming
  flags and Card JSON 2.0 remains explicitly enabled.

## Runtime boundary

The rollout changed only channel rendering, attachment delivery, and card interaction handling. Provider execution remains inside the pre-existing tmux panes; no tmux session was replaced by a headless provider process.

## 2026-07-11 button-free state-color update

- Deployed code commit `ea4b4d7` from `productization-prep` locally and on hbhy.
- `ruff check tmuxbot tests` passed; `pytest -q` passed with 159 tests and the same upstream lark-oapi deprecation warning.
- Local `tmuxbot.service` restarted active; local tmux session count remained 6 before and after restart.
- hbhy `tmuxbot.service` and `tmuxbot-codex.service` restarted active; remote tmux session count remained 21 before and after restart.
- Telegram Codex acceptance message `1042` was delivered with `reply_markup=None`.
- Claude Feishu acceptance message `om_x100b6a2d9de82cacc4908d1439ed2d0` and Codex Feishu acceptance message `om_x100b6a2d9df9dcb8c3793dd116c4fa7` were delivered as green Card JSON 2.0 cards with zero button elements.
- Codex Feishu dynamic-state canary `om_x100b6a2d9a79d880c3cf33ee10efd4d` was created yellow and successfully patched green.
- New replies now use slash commands as the sole operation entry point. Legacy callbacks remain only for already-sent messages.
- Both Feishu services resumed all configured tailers after restart with no startup traceback.

## 2026-07-11 Telegram state-badge update

- Confirmed against the official Telegram Bot API that Telegram has no Feishu-equivalent colored card header or selectable message background.
- Added text-native state badges: `🟡 工作中`, `🟠 等待输入`, `✅ 已完成`, `🔴 错误/阻塞`, `🔵 信息`, and `⚪ 状态未知`.
- Missing state remains unrendered, and Telegram replies remain free of persistent buttons.
- `ruff check tmuxbot tests` passed; `pytest -q` passed with 169 tests and one upstream lark-oapi deprecation warning.
- Local `tmuxbot.service` restarted active; tmux session count remained 6 before and after restart.
- Telegram Codex acceptance message `1043` displayed the completed-state badge and was delivered with `reply_markup=None`.

## 2026-07-11 channel control-panel rollout

- Added Chinese `/panel` and `/settings` entry points plus `/mention on|off|default|status` fallback commands.
- Binding-scoped mention policy is atomically persisted in `bindings.yaml`; Boss control commands bypass mention wake-up so the panel cannot lock out its owner.
- Panel actions cover `/status`, `/screen`, `/new` with confirmation, `/compact`, `/resume`, `/model`, `/esc`, and `/cc` while ordinary assistant replies remain button-free.
- Model switching uses each active tmux CLI's native `/model` picker. A temporary Codex canary switched from `gpt-5.6-sol high` to `gpt-5.6-terra medium` and confirmed the change in the live TUI.
- Claude Code 2.1.205 native picker was verified to expose default, Opus, Fable, Sonnet, and Haiku choices in the current account. Interaction cards include `仅本会话`, which sends Claude's native `s` shortcut instead of changing the future-session default.
- Telegram Codex control-panel acceptance message `1044` was delivered with six Chinese keyboard rows.
- Claude Feishu panel `om_x100b6a2ec6a084a8c2e8a300e9efc32` and Codex Feishu panel `om_x100b6a2ec6b6dca8c444b39d78f3228` were delivered as interactive Card JSON 2.0 messages.
- `ruff check tmuxbot tests` passed; `pytest -q` passed with 186 tests and one upstream lark-oapi deprecation warning.
- Local service remained active with 6 tmux sessions before and after restart. Both hbhy services remained active with 21 tmux sessions before and after restart.
- Expanded multi-LLM coordination research was recorded in English and Chinese only; no orchestration runtime code was added.

## 2026-07-11 CLI restart recovery / CLI 重启恢复

- Corrected restart semantics after review: a restart must retain the binding's existing provider session and transcript; creating and rebinding to a new Codex session loses conversation context and is not acceptable.
- 复审后修正重启语义：重启必须保留 binding 原有的 provider 会话与 transcript；新建并改绑 Codex 会话会丢失对话上下文，不符合要求。
- Codex CLI 0.144.1 supports `codex resume <SESSION_ID>`. The backend now launches the bound session with the unattended permission flag, matching the existing Claude `--resume` behavior.
- Codex CLI 0.144.1 支持 `codex resume <SESSION_ID>`。后端现在使用 binding 中的会话 ID 加无人值守权限参数恢复会话，与 Claude 现有的 `--resume` 语义一致。
- Telegram and Feishu retain the confirmed `重启 CLI` panel action. Lifecycle no longer clears or replaces provider identity during restart.
- Telegram 与飞书继续保留带二次确认的“重启 CLI”；生命周期层不再在重启时清除或替换 provider 身份。
- CliproxyApi live acceptance resumed session `019f450e-c966-7b51-b9c0-975d2b1acf7b`; the pane displayed its earlier release/model discussion, the original JSONL grew from 577132 to 603499 bytes, and Telegram received `✅ 已恢复旧会话上下文`.
- CliproxyApi 在线验收恢复了会话 `019f450e-c966-7b51-b9c0-975d2b1acf7b`；pane 中保留此前的版本/模型讨论，原 JSONL 从 577132 增长到 603499 字节，Telegram 收到 `✅ 已恢复旧会话上下文`。
- Local and hbhy Ruff checks passed with 190 tests. Local tmux remained at 6 sessions; both hbhy services stayed active with 21 sessions before and after restart.
- 本机与 hbhy 的 Ruff 检查及 190 项测试均通过；本机保持 6 个 tmux 会话，hbhy 两个服务均为 active，重启前后保持 21 个会话。
