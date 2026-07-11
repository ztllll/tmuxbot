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
