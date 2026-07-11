# Channel Control Panel Design / 通道轻量控制面板设计

## Goal / 目标

Add an explicitly opened, Chinese-language `/panel` control surface to Telegram and Feishu. It manages per-binding mention policy, exposes common tmux-backed CLI commands, and opens each provider's native model picker without restoring persistent controls on ordinary assistant replies.

为 Telegram 和飞书增加主动打开的中文 `/panel` 控制面板。面板管理每个 binding 的群聊 `@` 策略、提供常用 tmux/CLI 命令，并打开 provider 原生模型选择器；普通助手回复仍不显示常驻按钮。

## Entry points / 入口

- `/panel` is canonical; `/settings` is an alias.
- `/mention on|off|default|status` is the text fallback.
- Boss-only control commands bypass the mention wake-up requirement so a user cannot lock themselves out of the panel.
- Private chats ignore mention policy.

`/mention on` means mention-free operation is enabled; `/mention off` means the bot must be addressed. The panel labels make this direction explicit in Chinese to avoid ambiguity.

## Panel contents / 面板内容

The panel header and explanatory body show:

- Binding name, channel, provider, tmux target, and runtime mode.
- Effective group wake-up policy and whether it comes from the binding override or deployment default.
- A Chinese reminder that panel buttons operate the existing CLI inside tmux.

Interactive controls:

- Mention policy: `无需 @`, `必须 @`, `继承默认`.
- Read-only actions: `/status`, `/screen`.
- Session actions: `/new`, `/compact`, `/resume`.
- Model action: `切换模型`.
- TUI controls: `/esc`, `/cc`.
- Refresh and close panel.

`/new` uses a confirmation step. Feishu uses the native button confirmation dialog; Telegram replaces the panel keyboard with explicit confirm/back buttons.

## Model switching / 模型切换

The panel never stores a static model catalog. It dispatches `/model` to the active provider TUI through the existing command adapter:

- Codex opens its native model and reasoning-effort picker. Official Codex documentation states that `/model` changes the active model mid-session; `/status` verifies the result.
- Claude opens its native picker. Claude also accepts `/model <alias|name>` and applies the change immediately; native picker confirmation can also update its default for future sessions according to Claude Code behavior.
- The interaction card captures the tmux screen and provides navigation keys. Available choices therefore remain aligned with the installed CLI version and account entitlement.

No provider process is replaced, no SDK conversation is created, and the active tmux session remains the execution plane.

## Persistence / 持久化

`Binding.mention_required: bool | None` is loaded from and atomically saved to `bindings.yaml`:

```yaml
mention_required: true   # 必须 @
mention_required: false  # 无需 @
# missing/null           # inherit frontend deployment default
```

The in-memory binding is updated before the callback returns, so the policy changes immediately without a service restart.

## Security / 安全

- Telegram requires the configured Boss user ID and exact chat/topic binding.
- Feishu requires a Boss open ID and exact chat binding.
- Callback payloads use the existing stable binding token and chat-correlation checks.
- Only allowlisted panel actions can reach the dispatcher.
- Ordinary reply cards stay button-free; only `/panel`, confirmations, pickers, and explicit TUI interaction cards contain controls.

## Testing / 测试

- Pure tests for effective mention policy, command parsing, panel text, and atomic persistence.
- Telegram tests for `/panel` keyboard shape, mention-policy callback ACL, and confirmation behavior.
- Feishu tests for Card JSON 2.0 panel structure, callback updates, and interaction controls.
- Provider-neutral tests proving model action dispatches `/model` rather than a hardcoded model string.
- Full Ruff and pytest verification followed by live Telegram and Feishu acceptance without changing tmux session counts.
