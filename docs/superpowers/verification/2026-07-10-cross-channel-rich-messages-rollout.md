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

- Root cause: after Codex restarted inside the existing tmux pane, the binding still pinned the previous session JSONL. Telegram input reached the new CLI, but the tailer kept watching the ended transcript, so replies were not pushed back.
- 原因：Codex 在原 tmux pane 内重启后，binding 仍固定到旧会话 JSONL。Telegram 输入能进入新 CLI，但 tailer 继续监听已结束的 transcript，因此没有回推回复。
- Added a confirmed `重启 CLI` action to both Telegram and Feishu panels. The lifecycle layer now detects a newly launched provider, discovers its new transcript without the stale identity pin, and rebinds the in-memory identity for persistence by the tailer.
- Telegram 与飞书面板均新增带二次确认的“重启 CLI”。生命周期层会识别新启动的 provider，忽略旧身份固定项查找新 transcript，并重新绑定会话身份供 tailer 持久化。
- A CliproxyApi live canary produced `✅ CliproxyApi 重启后 TG 回推链路已恢复`; the new JSONL grew and both live/final assistant events were observed without Telegram send errors.
- CliproxyApi 在线验收返回 `✅ CliproxyApi 重启后 TG 回推链路已恢复`；新 JSONL 正常增长，live/final 助手事件均被捕获，未出现 Telegram 发送错误。
- Local Ruff and 189 tests passed. The local runtime remained one bot process with 6 tmux sessions. Both hbhy services restarted active at commit `0350e57`, with 21 tmux sessions before and after.
- 本机 Ruff 与 189 项测试通过；运行时保持单一 bot 进程和 6 个 tmux 会话。hbhy 两个服务在提交 `0350e57` 上重启后均为 active，tmux 会话数前后保持 21。
