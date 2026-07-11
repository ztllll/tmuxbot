# Telegram State Badges Design / Telegram 状态标识设计

## Goal / 目标

Document the presentation difference between Feishu and Telegram, then give Telegram a clean text-native equivalent of Feishu's state-colored card header without restoring persistent buttons.

明确记录飞书与 Telegram 的展示能力差异，并为 Telegram 增加简洁的文本状态标识，作为飞书彩色卡片标题的等价表达，同时不恢复常驻按钮。

## Platform boundary / 平台边界

Feishu Card JSON 2.0 supports semantic header templates such as yellow, orange, green, red, blue, and grey. Telegram Bot API messages do not expose an equivalent per-message colored card header. Telegram supports formatted text, message entities, expandable block quotes, message editing, reactions, typing actions, and optional reply markup, but the application cannot select a colored message background or header strip.

飞书 Card JSON 2.0 可以直接设置黄色、橙色、绿色、红色、蓝色和灰色标题模板。Telegram Bot API 没有等价的“消息彩色标题栏”能力；它提供富文本、可展开引用、消息编辑、reaction、typing 和可选键盘，但机器人不能指定消息背景色或标题条颜色。

## Telegram equivalent / Telegram 等价方案

`render_telegram_document()` renders one status line between the reply header and body whenever `ReplyDocument.state` is known:

| State | Telegram label | Feishu template |
| --- | --- | --- |
| `working` | `🟡 工作中` | yellow |
| `waiting` | `🟠 等待输入` | orange |
| `completed` / `idle` | `✅ 已完成` | green |
| `error` / `blocked` / `dead` | `🔴 错误/阻塞` | red |
| `info` | `🔵 信息` | blue |
| explicit unknown value | `⚪ 状态未知` | grey |

The label uses bold Telegram HTML. A missing state emits no status line, preserving compatibility for generic messages. New replies remain button-free and slash commands remain the only operation interface.

状态行使用 Telegram HTML 粗体。没有状态信息的普通消息不额外显示状态行，以保持兼容。新回复仍然无按钮，操作继续统一使用斜杠命令。

## Data flow / 数据流

The provider-neutral `ReplyEnvelope.metadata["display_state"]` remains the source of presentation state. `build_reply_document()` already normalizes that value. Feishu maps it to `header.template`; Telegram maps the same value to an Emoji plus Chinese label. No provider-specific parsing is added to either channel renderer.

## Testing and documentation / 测试与文档

- Add parameterized renderer tests covering every state mapping and missing-state behavior.
- Keep the existing no-button Telegram delivery regression test.
- Update README and the Chinese state-color guide to explain native Feishu colors versus Telegram text badges.
- Run Ruff and the complete pytest suite before deployment.

## Non-goals / 非目标

- Do not simulate colored cards by generating images.
- Do not restore Telegram Inline Keyboard or Feishu buttons.
- Do not change tmux sessions, provider event ingestion, attachments, or command routing.
