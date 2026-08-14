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
TG_OMP_BOT_TOKEN=123456789:replace-me
BOSS_USER_ID=123456789
CLAUDE_BIN=/home/you/.local/bin/claude
CODEX_BIN=/home/you/.local/bin/codex
OMP_BIN=/home/you/.local/bin/omp
```

说明：

- `TG_OMP_BOT_TOKEN` 是 OMP route 的默认 Telegram credential；credential identity 与 backend 相互独立，已有 route 显式设置的 `bot_token_env` 应原样保留。
- `BOSS_USER_ID` 是唯一允许操作 route 的 Telegram 用户 ID。可先设为 `0`，通过私聊第一条消息进入一次性 setup mode；生产环境应固定为真实 ID。
- 多个 Telegram Bot 使用独立变量名，例如 Claude Code 的 `TG_BOT_TOKEN`、Codex 的 `TG_CODEX_BOT_TOKEN`，并在 route 的 `bot_token_env` 中引用。
- 群/topic 是否必须 `@bot` 优先由 route 的 `mention_required` 控制；全局默认使用 `TELEGRAM_GROUP_MENTION_ONLY=true|false`，credential 级可使用 `<TOKEN_ENV>_GROUP_MENTION_ONLY`，以 `_BOT_TOKEN` 结尾的变量也兼容去掉该后缀的短名（例如 `TG_OMP_GROUP_MENTION_ONLY`）。

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
  - name: demo-omp
    channel: telegram
    bot_token_env: TG_OMP_BOT_TOKEN
    chat_id: -1001234567890
    thread_id: 8024
    tmux_session: demo-omp
    tmux_window: 0
    tmux_pane: 0
    cwd: /home/you/projects/demo
    backend: omp
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
| `backend` | `claude_code`、`codex` 或 `omp` |
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
  --name demo-omp \
  --channel telegram \
  --credential TG_OMP_BOT_TOKEN \
  --topic-link https://t.me/c/INTERNAL_CHAT_ID/THREAD_ID \
  --cwd /home/you/projects/demo \
  --backend omp

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

## 7. OMP 专属行为

Oh My Pi（运行时命令 `omp`）始终运行真实交互式 TUI，不使用 RPC、SDK 或 print/headless mode。tmuxbot 通过 provider registry 启动它，固定参数为：

```text
omp --approval-mode yolo --extension <受管扩展的绝对路径>
```

浏览器、route YAML 和 Admin 请求都不能提交 binary path、tmux target 对应的任意 argv 或自定义启动参数；可执行文件只按 `OMP_BIN` → `PATH` → `~/.local/bin/omp` 在服务端解析。受管扩展随 tmuxbot 打包，并在每次启动时用 `--extension` 显式加载，不依赖用户扩展发现目录。

OMP 会话通常位于 `~/.omp/agent/sessions/<project-key>/<timestamp>_<id>.jsonl`。当前精确会话身份由扩展同时写入：

- `$TMUXBOT_STATE_DIR/omp-session-handoffs/`：`tmuxTarget/cwd/sessionId/transcriptPath/processId`；
- `$TMUXBOT_STATE_DIR/omp-session-health/`：同一会话身份及 `idle/working/recovering/terminal_error`。

OMP 新 session 的 JSONL 可能到首条消息才创建。文件尚不存在时，tmuxbot 只接受受管扩展写入、位于官方 `~/.omp/agent/sessions` root、文件名匹配 session ID，且 `processId` 确属 exact pane 的 pending identity；文件出现后立即要求 header/cwd/session ID 全部吻合。已有 pin 只使用 `omp ... --resume <绝对 JSONL 路径>` 恢复，不使用 `--continue`、`--session` 或按 mtime 猜测会话。pin 不存在、header/cwd 不匹配、OMP 恢复失败或新启动未发布匹配 sidecar 时，tmuxbot 保留原 pin 并显式失败，绝不会静默开启新会话覆盖连续性。

`/restart` 对 OMP 执行干净的 pane respawn，再按上述固定参数和现有精确 pin 启动；它不会向原生 TUI 注入 Ctrl-C/Ctrl-D。正在运行的 OMP 若缺少有效受管 handoff，也会 fail closed，并要求通过 `/restart` 或 Web 重新启动受管 OMP。

推荐 tmux 配置：

```tmux
set -g extended-keys on
set -g extended-keys-format csi-u
```

OMP 原生菜单、picker、ask、approval、plan review、文本输入和确认界面采用 **SSH-only**：tmuxbot 只向原 endpoint 提示精确 `SESSION:WINDOW.PANE`，不从 Telegram/飞书发送方向键、Enter、Escape、批准或取消。bot `/plan` 只是本地帮助；它不会把 `/plan` 注入 OMP，而是提示操作者 SSH attach 后使用默认 `Alt+Shift+P`（自定义 keybindings 可能不同）。

只读终端健康审计默认启用，可显式配置：

```dotenv
TMUXBOT_OMP_TERMINAL_HEALTH_ENABLED=1
TMUXBOT_OMP_TERMINAL_HEALTH_INTERVAL=600
TMUXBOT_OMP_TERMINAL_HEALTH_FILE=/home/you/.local/state/tmuxbot/omp-terminal-health.json
```

该审计只通知 provider-authored terminal error 或多信号疑似失活，不自动 `/restart`、`/new`、停止或杀进程。完整设计见 [`omp-terminal-error-audit.md`](omp-terminal-error-audit.md)。

## 8. 附件与 Web 安全

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

## 9. 常用检查

```bash
tmuxbot doctor
tmuxbot route --file ~/.config/tmuxbot/bindings.yaml validate
systemctl --user is-active tmuxbot.service
journalctl --user -u tmuxbot.service -n 100 --no-pager
```

日志应看到对应 credential frontend、每条 route 的 `tailer start/alive`，以及需要时的：

```text
OMP terminal-health audit starting · interval=600.0s
lifecycle watchdog starting
```

若现有 OMP pane 没有匹配的受管身份，日志/错误会明确报告“缺少有效的受管会话身份 sidecar”，而不是选择同 cwd 下最新的 JSONL。

生产迁移、offset 防历史回吐和单服务合并见 [`single-service-operations.md`](single-service-operations.md)。
