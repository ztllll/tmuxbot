# 配置与运行说明

本页说明 tmuxbot 0.3.x 的配置文件位置、Telegram/飞书 credential、精确 route、systemd 和常用运行参数。首次部署优先使用 WebUI；只有需要批量维护、迁移或审计时才手工编辑配置。

## 1. 安装与首次启动

```bash
uv tool install 'tmuxbot[full]'
tmuxbot doctor
tmuxbot serve --open
```

`serve` 同时运行本地 WebUI 和受监督的 bridge child。默认监听 `127.0.0.1:8765`，不要直接暴露到公网。

默认运行目录遵循 XDG：

| 内容 | 默认位置 | 覆盖变量 |
|---|---|---|
| credential 与 route | `~/.config/tmuxbot/` | `TMUXBOT_CONFIG_DIR` |
| `.env` | `~/.config/tmuxbot/.env` | `TMUXBOT_ENV` |
| `bindings.yaml` | `~/.config/tmuxbot/bindings.yaml` | `TMUXBOT_BINDINGS` |
| 数据库 | `~/.local/share/tmuxbot/control-plane.sqlite3` | `TMUXBOT_DATABASE` |
| offsets/健康状态/lock | `~/.local/state/tmuxbot/` | `TMUXBOT_STATE_DIR` |

源码 checkout 根目录已有 `.env` / `bindings.yaml` 时仍兼容，但新部署建议使用上述 XDG 路径。配置目录会被收紧为 `0700`；credential 和 bindings 不得提交 Git。

## 2. 最小 Telegram 配置

创建 `~/.config/tmuxbot/.env`：

```dotenv
TG_BOT_TOKEN=123456789:replace-me
BOSS_USER_ID=123456789
CLAUDE_BIN=/home/you/.local/bin/claude
CODEX_BIN=/home/you/.local/bin/codex
PI_BIN=/home/you/.local/bin/pi
```

说明：

- `TG_BOT_TOKEN` 是 BotFather token；同一个 token 可以承载 Claude Code、Codex 和 Pi route。
- `BOSS_USER_ID` 是唯一允许操作 route 的 Telegram 用户 ID。可先设为 `0`，通过私聊第一条消息进入一次性 setup mode；生产环境应固定为真实 ID。
- 多个 Telegram Bot 使用新的变量名，例如 `TG_CODEX_BOT_TOKEN`，并在 route 的 `bot_token_env` 中引用。
- 群/topic 是否必须 `@bot` 优先由 route 的 `mention_required` 控制；全局默认使用 `TELEGRAM_GROUP_MENTION_ONLY=true|false`，credential 级可使用 `<TOKEN_ENV>_GROUP_MENTION_ONLY`，以 `_BOT_TOKEN` 结尾的变量也兼容去掉该后缀的短名（例如 `TG_CODEX_GROUP_MENTION_ONLY`）。

Telegram forum topic route 只需要 `chat_id + thread_id`。可使用：

```text
https://t.me/c/INTERNAL_CHAT_ID/THREAD_ID
https://t.me/c/INTERNAL_CHAT_ID/THREAD_ID/MESSAGE_ID
```

不要猜测 thread ID；不确定时提供话题 URL，并使用 `tmuxbot admin provision-project` 解析和绑定。

## 3. 最小飞书配置

每套飞书 App 使用一个 credential 前缀。route 设置 `bot_token_env: FEISHU` 时读取：

```dotenv
FEISHU_APP_ID=cli_xxx
FEISHU_APP_SECRET=replace-me
FEISHU_BOSS_OPEN_IDS=ou_xxx
# 可选：用于群消息 @bot 识别
FEISHU_BOT_OPEN_ID=ou_bot_xxx
```

第二套 App 可使用 `FEISHU_CODEX`：

```dotenv
FEISHU_CODEX_APP_ID=cli_xxx
FEISHU_CODEX_APP_SECRET=replace-me
FEISHU_CODEX_BOSS_OPEN_IDS=ou_xxx,ou_yyy
```

飞书使用 WebSocket 长连接；应用需要启用机器人与相应消息事件。若启用 CardKit 流式更新，还需订阅 `card.action.trigger` 并授予 `cardkit:card:write`：

```dotenv
TMUXBOT_FEISHU_CARD_V2=1
TMUXBOT_FEISHU_STREAMING=0
# credential 级覆盖：FEISHU_CARD_V2 / FEISHU_STREAMING
```

飞书 thread route 必须持久化 `thread_root_message_id`，否则 bridge 重启后无法保证回复仍回到精确 thread，系统会 fail closed，绝不会漏发到群根。

credential 级开关使用 `<bot_token_env>_CARD_V2` 与 `<bot_token_env>_STREAMING`。例如 route 使用 `bot_token_env: FEISHU_CODEX`，则配置 `FEISHU_CODEX_CARD_V2` / `FEISHU_CODEX_STREAMING`。

## 4. Route / bindings.yaml

一个 route 把一个精确 IM endpoint 映射到一个精确 tmux pane：

```yaml
bindings:
  - name: demo-pi
    channel: telegram
    bot_token_env: TG_BOT_TOKEN
    chat_id: -1001234567890
    thread_id: 8024
    tmux_session: demo-pi
    tmux_window: 0
    tmux_pane: 0
    cwd: /home/you/projects/demo
    backend: pi
    mention_required: false
```

稳定字段：

| 字段 | 含义 |
|---|---|
| `name` | 全局唯一 route 名称 |
| `channel` | `telegram` 或 `feishu` |
| `bot_token_env` | credential 环境变量名/前缀 |
| `chat_id` / `thread_id` | 精确 IM endpoint；群根与每个 topic/thread 都不同 |
| `thread_root_message_id` | 飞书 thread 的稳定回复锚点；Telegram 不需要 |
| `tmux_session/window/pane` | 唯一 tmux target |
| `cwd` | provider TUI 的绝对工作目录 |
| `backend` | `claude_code`、`codex` 或 `pi` |
| `mention_required` | 群内是否必须 @bot；项目 topic 通常设为 `false` |

约束：

1. endpoint 唯一：`channel + credential + chat_id + thread_id` 不得重复；
2. tmux target 唯一：两个 route 不得绑定同一 pane；
3. 同一 pane 不得复用不同 cwd；
4. 未绑定 endpoint 完全静默，不打反应、不回复，也不触碰 tmux；
5. `provider_session_id`、`transcript_path` 等身份字段由 tmuxbot 持久化，不建议手工填写。

校验：

```bash
tmuxbot route --file ~/.config/tmuxbot/bindings.yaml validate
tmuxbot route --file ~/.config/tmuxbot/bindings.yaml list --json
```

完整示例见 [`../bindings.example.yaml`](../bindings.example.yaml)。

## 5. 推荐的确定性开通流程

普通项目开通不要手工拼 YAML、tmux 和 systemd 事务。先 plan，再使用完全相同的命令加 `--apply`：

```bash
tmuxbot admin \
  --file ~/.config/tmuxbot/bindings.yaml \
  --service tmuxbot.service \
  provision-project \
  --name demo-pi \
  --channel telegram \
  --credential TG_BOT_TOKEN \
  --topic-link https://t.me/c/INTERNAL_CHAT_ID/THREAD_ID \
  --cwd /home/you/projects/demo \
  --backend pi

# 核对 endpoint、cwd、adapter、target 后，原命令末尾增加：
# --apply
```

新建 Telegram/飞书 topic 时，用精确 `--chat-id` 与 `--topic-title` 替换 `--topic-link`。该事务负责 endpoint 解析/创建、exact-cwd tmux target、完整 route 校验、原子写入、bridge 重启、verify 和失败回滚。详细说明见 [`admin-dm-operations.md`](admin-dm-operations.md)。

## 6. systemd 生产部署

```bash
tmuxbot install-service --now
loginctl enable-linger "$USER"
systemctl --user status tmuxbot.service
journalctl --user -u tmuxbot.service -f
```

每台主机通常只运行一个 `tmuxbot.service`；一个 bridge 可以承载多套 Telegram Bot 和飞书 App。不要因为增加 credential 就复制 service。

安装器默认启用每小时一次的非破坏性 provider 健康审计：

```dotenv
TMUXBOT_LIFECYCLE_ENABLED=1
TMUXBOT_LIFECYCLE_INTERVAL=3600
```

它只检查已存在 pane，不按空闲退出 provider，也不会复活被人工关闭的 tmux。缺失 route target 在下一条精确 endpoint 消息到达时按需恢复。`--self-heal` 仅保留为兼容参数。

## 7. Pi 专属行为

Pi 保持真实交互 TUI，不使用 RPC/SDK/print mode。推荐 tmux 配置：

```tmux
set -g extended-keys on
set -g extended-keys-format csi-u
```

Pi 菜单、选择、文本输入和确认界面采用 **SSH-only**：tmuxbot 只有在当前屏幕底部控制提示紧邻实时 Pi footer 时才向原 endpoint 告警，并给出精确 `SESSION:WINDOW.PANE`。IM 不发送方向键/选择卡，也不模拟 Enter、Escape、批准或取消；旧卡 callback 同样会拒绝。

可选的只读失活审计：

```dotenv
TMUXBOT_PI_TERMINAL_HEALTH_ENABLED=1
TMUXBOT_PI_TERMINAL_HEALTH_INTERVAL=600
```

该审计不会自动 `/restart`、`/new`、停止或杀进程。

## 8. IM 结果优先展示

默认 `compact` 模式把一个 provider Turn 压缩为至多一张可编辑过程卡和一条独立最终结果；短于延迟阈值的任务只发最终结果。

```dotenv
TMUXBOT_IM_PRESENTATION=compact
TMUXBOT_IM_PROGRESS_DELAY=4
TMUXBOT_IM_PROGRESS_UPDATE_INTERVAL=2
TMUXBOT_IM_PROGRESS_MAX_STEPS=3
```

- `result_only`：隐藏普通工具、计划和生命周期过程；用户交互、阻塞/失败和最终结果仍独立通知。
- `compact`：超过延迟阈值后创建一张过程卡，按内容变化和节流窗口原地更新，最终压缩为摘要。
- `verbose`：立即创建过程卡，但仍复用同一 message ID，不回吐终端日志。

`TEXT_DELTA` 和 provider live text 只写入内存 ResultDraft；`FINAL_TEXT` 统一经过 ReplyEnvelope、通道分页、附件和 footer 后发布一次。过程卡 edit/PATCH 失败会创建替代摘要，不阻断或重复最终结果。

内容无关的计数默认写入 `$XDG_STATE_HOME/tmuxbot/im-delivery-audit.json`，仅包含 create/edit/finalize/result/attention 次数与结果字符数，不记录正文。可通过 `TMUXBOT_IM_DELIVERY_AUDIT_FILE` 覆盖路径。

## 9. 附件与 Web 安全

```dotenv
TMUXBOT_ATTACHMENT_DIR=/tmp/tmuxbot-attachments
TMUXBOT_ATTACHMENT_ALLOWED_ROOTS=/srv/reports:/home/you/shared

TMUXBOT_WEB_HOST=127.0.0.1
TMUXBOT_WEB_PORT=8765
TMUXBOT_WEB_SECURE_COOKIE=false
# 反向代理 HTTPS 后：
# TMUXBOT_WEB_PUBLIC_ORIGIN=https://tmuxbot.example.com
# TMUXBOT_WEB_SECURE_COOKIE=true
```

AI 回复里的本地文件仅允许从 route cwd、attachment 目录、系统临时目录和显式 trusted roots 上传。目录、设备、socket、不存在文件和白名单外路径都会拒绝。

WebUI 默认只能监听 localhost。远程访问必须使用带 TLS 和身份认证的反向代理，不要直接开放 `8765`。

## 10. 常用检查

```bash
tmuxbot doctor
tmuxbot route --file ~/.config/tmuxbot/bindings.yaml validate
systemctl --user is-active tmuxbot.service
journalctl --user -u tmuxbot.service -n 100 --no-pager
```

日志应看到对应 credential frontend、每条 route 的 `tailer start/alive`，以及需要时的：

```text
managed Pi handoff extension installed
Pi terminal-health audit starting
lifecycle watchdog starting
```

生产迁移、offset 防历史回吐和单服务合并见 [`single-service-operations.md`](single-service-operations.md)。
