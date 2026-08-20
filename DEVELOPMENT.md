# tmuxbot — 开发文档

> Telegram + 飞书 ↔ tmux AI CLI (Claude Code / Codex / Pi) 双向桥。精确话题路由 + 可插拔 adapter 架构。
> 决策依据见 `RESEARCH.md`, 代码审查见 `CODE_REVIEW.md`, 变更历史见 `CHANGELOG.md`, 版本策略见 `VERSIONING.md`, 发布流程见 `RELEASE.md`, 项目宪法见 `CLAUDE.md`。

---

## 1. 目标

让 Boss 在 Telegram/飞书精确端点 (DM / 群话题 / thread) 发消息 → 注入对应 tmux pane 内的真实 CLI → 输出实时回推同端点。
每个 route 绑定一个 pane、cwd 和 adapter；同一 credential 可承载不同 CLI。**bot 只搬键盘 + 屏幕**,不调 vendor API/SDK/headless。

---

## 2. 文件结构

```
tmuxbot/                       ← 仓库根
├── tmuxbot.py                 ← thin entry (~16 行) 调 tmuxbot package
├── bin/                       ← 运维脚本
│   ├── restart.sh             ← 重启 (含失败自动重试 3 次)
│   ├── stop.sh                ← 优雅停 (TERM → KILL)
│   └── status.sh              ← 看进程 / session / 日志
├── deploy/
│   └── systemd/
│       ├── tmuxbot.service     ← IM bridge systemd user unit
│       └── tmuxbot-web.service ← Web control plane 独立 unit
├── tmuxbot/                   ← Python package
│   ├── __init__.py
│   ├── __main__.py            ← CLI 与 bridge 装配入口
│   ├── core/                  ← provider/channel 事件、消息、回复与 Runtime V2 契约
│   ├── control_plane/         ← SQLite migration/repository（IM TeamRun 等后台能力）
│   ├── providers/             ← CLI discovery、能力与统一启动参数
│   ├── runtime/               ← 串行 tmux runtime/input queue
│   ├── teamrun/               ← DAG、worker、worktree、mailbox、artifact、scheduler（IM/后台保留）
│   ├── web/                   ← FastAPI Terminal Wall、tmux window inventory 与静态 WebUI
│   ├── channels/              ← 通道传输契约与 Telegram/飞书适配器
│   ├── hooks/                 ← Claude hook 安装与本地 spool
│   ├── state.py               ← Binding + State + fire()
│   ├── config.py              ← .env + bindings.yaml + offsets.json → State
│   ├── utils.py               ← encode_cwd / cwidth / render_table / offsets debounced
│   ├── attachments.py         ← IM 附件落盘、文件名清洗、@path 注入 prompt、出站路径识别
│   ├── tmux.py                ← tmux_send_text (async) / send_key / capture / pane_command
│   ├── picker.py              ← PICKER_BOTTOMBAR_RE / detect_idle_picker
│   ├── jsonl.py               ← jsonl_poll_loop + on_tmux_event (含 tool_aggregator / plan_messages / 积压保护)
│   ├── heartbeat.py           ← heartbeat_typing_loop (TUI 指纹判活跃)
│   ├── commands.py            ← capture_and_push (slash 注入 + 屏幕等待 + 结构化反馈)
│   ├── dispatch.py            ← 共享命令分发层 (TG/飞书共用 stop/capture/text 逻辑)
│   ├── quota.py               ← OAuth API 订阅配额 (5h/7d 五窗口 + 重置倒计时)
│   ├── backends/
│   │   ├── base.py            ← Backend ABC + CmdOpts
│   │   ├── claude_code.py     ← ClaudeCodeBackend: parse_event / parse_* / find_active_jsonl
│   │   │                         / ensure_running / find_tui_activity_fp / aggregate_usage
│   │   ├── codex.py           ← CodexBackend
│   │   └── pi.py              ← PiBackend: 原生/自定义 statusline 解析 + JSONL metadata/usage/todo 补全
│   └── frontends/
│       ├── base.py            ← Frontend ABC 与回复发送契约
│       ├── telegram.py        ← TelegramFrontend: aiogram + ACL + handlers
│       ├── feishu.py          ← FeishuFrontend: lark-oapi WebSocket + Card JSON 2.0
│       └── feishu_cards.py    ← 飞书卡片构建与分页
├── webui/                     ← React/Vite/xterm.js 中文控制台源码
├── bindings.yaml              ← 绑定配置 (gitignored; 多实例 bindings*.yaml 也忽略)
├── .env                       ← TG_BOT_TOKEN / TG_CODEX_BOT_TOKEN / BOSS_USER_ID 等 (gitignored)
├── .env.example
├── .gitignore
├── pyproject.toml             ← aiogram>=3.13, pyyaml>=6.0, python-dotenv>=1.0; lark-oapi>=1.4 optional
├── VERSIONING.md              ← 版本号/分支/tag 策略
├── RELEASE.md                 ← 发布检查清单
├── CONTRIBUTING.md            ← 贡献与 PR 约定
├── SECURITY.md                ← 安全边界与敏感文件规则
├── SUPPORT.md                 ← issue/support 信息收集指南
├── data/                      ← gitignored; 多实例 data*/ 也忽略
│   ├── offsets.json           ← jsonl byte offset 持久化 (debounced 5s)
│   ├── tmuxbot.log
│   └── tmuxbot.lock
├── CHANGELOG.md               ← 变更历史
├── CLAUDE.md                  ← 项目宪法 + §9 决策日志
├── DEVELOPMENT.md             ← 本文件
├── CODE_REVIEW.md             ← P2 地毯审查
├── RESEARCH.md                ← 立项调研
├── README.md                  ← 入口
└── LICENSE                    ← MIT
```

---

## 3. 架构说明

### 多前端 × 多后端矩阵

```
                     ┌─────────────────────────────────────┐
                     │           tmuxbot/__main__.py         │
                     │   装配: 按 channel/token 分拣         │
                     └──┬──────────────────────┬────────────┘
                        │                      │
              ┌─────────▼─────────┐  ┌─────────▼──────────┐
              │  TelegramFrontend  │  │   FeishuFrontend    │
              │  (aiogram polling) │  │  (lark-oapi WebSocket│
              │  ACL: user_id +    │  │  ACL: open_id +     │
              │  source_key        │  │  chat_id in bindings│
              └────────┬──────────┘  └──────────┬──────────┘
                       │  dispatch.py (共享层)   │
                       └────────────┬────────────┘
                                    │
              ┌─────────────────────▼──────────────────────┐
              │            dispatch_incoming_text            │
              │  stop / capture 命令 / /screen /info        │
              │  /restart / rename pending / 普通文本        │
              └──┬─────────────────────────┬───────────────┘
                 │                         │
    ┌────────────▼──────────┐  ┌──────────▼────────────┐
    │  ClaudeCodeBackend     │  │    CodexBackend        │
    │  parse_event / jsonl   │  │    parse_event / jsonl │
    │  ensure_running        │  │    ensure_running      │
    │  TUI 指纹 / compact    │  │                        │
    └────────────┬──────────┘  └──────────┬────────────┘
                 │                         │
    ┌────────────▼─────────────────────────▼────────────┐
    │              tmux pane (各 binding 独立)            │
    │  TUI idle → paste-buffer → composer 渲染与提交确认  │
    │  jsonl tailer → parse_event → aggregator → 推前端  │
    └───────────────────────────────────────────────────┘
```

**架构原则**:frontend 先按 `(channel, credential, chat_id, thread_id)` 命中 route，再以 `frontend.backend_for(binding)` 选择 Claude/Codex/Pi adapter。credential 只划分 Bot/App 身份，不决定 CLI 类型。群根与未绑定 topic/thread 完全静默；新增 topic route 通过 YAML、`tmuxbot route bind` 或 Admin DM 显式创建，不由群内 `/init` 隐式开通。

完整设计、配置和兼容迁移见 [`docs/topic-routing.md`](docs/topic-routing.md)。Boss 在 Admin DM 中用自然语言创建/绑定 tmux 与 Telegram/飞书话题的模板和验收流程见 [`docs/admin-dm-operations.md`](docs/admin-dm-operations.md)。低层配置操作仍使用 `tmuxbot route list|inspect|validate|bind|unbind`；普通 Admin LLM 的项目开通只使用 `tmuxbot admin provision-project`：一个 topic intent（新建标题、Telegram topic URL、或精确 chat/thread）加 route name/cwd/backend，即可获得固定 plan → apply → verify 流程；tmux 默认 `NAME:0.0`，不存在时事务创建、存在时只复用 exact-cwd pane。`inventory|telegram-topic|feishu-topics|create-topic|bind-topic|move-topic|adopt-pi-session|verify` 保留为低层诊断、恢复和迁移接口。若操作者直接在 Pi TUI 切换会话，route 仍钉在旧 JSONL 导致回推停止时，必须先用精确的 session JSONL 路径执行 `adopt-pi-session` plan，再带 `--apply` 原子认领已校验的同 cwd Pi 会话；该命令会重启 bridge 并验证 route/tmux/service，不能手改 YAML。Telegram route 只需要 `chat_id + thread_id`，`https://t.me/c/CHAT/THREAD` 已足够，不能额外要求 message ID 或 `thread_root_message_id`。直接编辑 YAML 仍作为离线恢复能力保留。

---

## 4. 飞书前端

### 依赖

```
lark-oapi>=1.4    # pip install lark-oapi  或  pip install -e ".[feishu]"
```

没装时其他前端正常启动,只有实际使用飞书 binding 时才报 `ImportError`。

### 消息格式

飞书不支持 HTML 消息,tmuxbot 内部 HTML(Telegram 格式)经 `_html_to_feishu_md` 转成飞书 Markdown:

| Telegram HTML | 飞书 Markdown |
|---|---|
| `<b>...</b>` / `<strong>` | `**...**` |
| `<i>...</i>` / `<em>` | `*...*` |
| `<s>...</s>` / `<del>` | `~~...~~` |
| `<code>...</code>` | `` `...` `` |
| `<pre>...</pre>` | ` ```\n...\n``` ` |

所有消息以 **interactive card** 形式发送(设 `update_multi=True`),支持 PATCH 就地编辑——与 TelegramFrontend 的 `edit_message_text` 对等,工具调用聚合器可复用。长 assistant 回复必须同时按 Card JSON 2.0 的 30KB payload 限制和每卡最多 50 个 body element 分片；只按 bytes 分片会让大量短 Markdown block 触发飞书 `230099 / element exceeds the limit`，legacy 单卡 fallback 又可能触发 `230025 / message content reaches its limit`。

### typing 状态

飞书无对等 API,`send_chat_action` 为 no-op。heartbeat_typing_loop 仍然调用,但无视觉效果。

### ACL

- `open_id` 在 `FEISHU_BOSS_OPEN_IDS` 白名单(逗号分隔)
- 精确 `(chat_id, thread_id)` 在本前端的 bindings 子集中
- 未配置的 source 会在日志中打印 `chat_id` 提示(便于接入新群),然后**完全静默**

### 同机多飞书 app(重要踩坑)

**lark-oapi 模块级全局 event loop**:SDK 内部在模块级保存一个 loop 引用,单进程内启动第二个飞书 ws client 会报 `"loop already running"`。

**解法:每个飞书 app 跑独立进程 + 独立 data 目录**:

```bash
# claude-feishu 进程
TMUXBOT_DATA_DIR=/data/claude-feishu TMUXBOT_BINDINGS=/etc/tmuxbot/claude-feishu.yaml python3 tmuxbot.py

# codex-feishu 进程
TMUXBOT_DATA_DIR=/data/codex-feishu TMUXBOT_BINDINGS=/etc/tmuxbot/codex-feishu.yaml python3 tmuxbot.py
```

对应 systemd service 用不同的 unit 文件,各自覆盖 `TMUXBOT_DATA_DIR` 和 `TMUXBOT_BINDINGS` 环境变量。

### 双向附件

Telegram 图片/文档/视频/动图/音频/语音与飞书图片/图文/文件会先下载到本机,再以 `@path` 注入对应 tmux TUI。默认附件目录为 `/tmp/tmuxbot-attachments`,可用 `TMUXBOT_ATTACHMENT_DIR` 覆盖。飞书下载图片/文件资源需要 app 开通 `im:resource` 权限。

AI CLI 回复中若出现独立一行的本地文件路径 (`@/abs/path`、`/abs/path`、`file:///abs/path`),jsonl 回推会移除该路径行并调用 frontend 原生附件接口发送:Telegram 使用 `send_photo`/`send_document`;飞书先上传 `/im/v1/images` 或 `/im/v1/files`,再以 `msg_type=image/file` 发送消息。路径识别也兼容 tmux 屏幕边框/提示符前缀,例如 `│ @/tmp/a.jpg`。飞书出站上传同样需要 `im:resource` / `im:resource:upload`,并需要消息发送权限。

### Codex 计划与可见工具事件

Codex `update_plan` 在 rollout jsonl 中表现为 `response_item.payload.type=function_call,name=update_plan`,完整计划在 `arguments.plan`。该事件不能只进普通 `assistant_tools` 聚合器,否则 IM 端只能看到工具日志里一闪而过的片段。当前路由:

- `CodexBackend.parse_event(update_plan)` → `("assistant_plan", rendered_plan)`
- `jsonl.on_tmux_event("assistant_plan")` → `state.plan_messages[binding.name]`
- 第一次计划更新发送一条“当前计划”消息;后续更新用 `edit_html` 编辑同一条消息

计划渲染包含 explanation、最多 12 条 step 和原始 status (`completed` / `in_progress` / `pending`)。这样 TG/飞书端始终能看到最新任务状态,用于判断当前是否仍在执行。

Codex 的 FREEFORM 工具不会总是走 `function_call`。例如 `apply_patch` 会落为 `response_item.payload.type=custom_tool_call,name=apply_patch`,补丁结果另有 `event_msg.payload.type=patch_apply_end`。这些事件需要给 IM 端短摘要:

- `custom_tool_call apply_patch` → `✂️ 改文件 <file>`
- `patch_apply_end success=true` → `✓ 改文件成功 <file>`
- `custom_tool_call_output` / 普通 `function_call_output` 默认不全文回推,避免大段命令输出刷屏;失败时才发短摘要

---

## 5. `bindings.yaml` schema

```yaml
bindings:
  # Telegram DM binding
  - name: proj-alpha
    channel: telegram              # 前端渠道: telegram (默认) / feishu
    chat_id: 123456789             # TG: int (DM = user_id; group = 负数)
    thread_id: null                # DM 无; forum topic 填 topic_id
    bot_token_env: TG_BOT_TOKEN    # 用哪个 bot token (env 变量名)
    backend: claude_code           # claude_code / codex / pi
    tmux_session: "claude-alpha"
    tmux_window: 0
    tmux_pane: 0
    cwd: /home/you/projects/alpha

  # Telegram supergroup forum topic binding
  - name: proj-beta-topic
    channel: telegram
    chat_id: -1001234567890        # supergroup: -100 前缀
    thread_id: 42                  # topic_id
    bot_token_env: TG_BOT_TOKEN
    backend: claude_code
    tmux_session: "claude-beta"
    tmux_window: 0
    tmux_pane: 0
    cwd: /home/you/projects/beta

  # 飞书 binding
  - name: proj-gamma-feishu
    channel: feishu
    chat_id: "oc_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"   # 飞书 chat_id
    thread_id: "omt_xxx"           # 飞书 thread 字符串; 群根/私聊填 null
    bot_token_env: FEISHU          # 读 FEISHU_APP_ID / FEISHU_APP_SECRET
    backend: claude_code
    tmux_session: "claude-gamma"
    tmux_window: 0
    tmux_pane: 0
    cwd: /home/you/projects/gamma
```

**对应 `.env` Telegram 唤醒变量**:

```bash
# 可选: 群/话题消息是否仅 @bot 才进入 tmux; DM 不受影响
TELEGRAM_GROUP_MENTION_ONLY=true

# 可选: 按 token 分别控制
TG_GROUP_MENTION_ONLY=true
TG_CODEX_GROUP_MENTION_ONLY=true
```

**对应 `.env` 飞书相关变量**:

```bash
# bot_token_env=FEISHU → 读这三个变量
FEISHU_APP_ID=cli_xxxxxxxxxxxxxxxx
FEISHU_APP_SECRET=xxxxxxxxxxxxxxxxxxxxxxxxxxxx
FEISHU_BOSS_OPEN_IDS=ou_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx  # 逗号分隔多个
FEISHU_BOT_OPEN_ID=ou_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx     # 可选: bot 自己 open_id, 用于 @ 唤醒

# 可选: 群消息是否仅 @bot 才响应 (默认 false,即不需要 @)
# FEISHU_GROUP_MENTION_ONLY=false
```

**校验红线**: 完整 endpoint `(channel, bot_token_env, chat_id, thread_id)` 唯一; 完整 tmux target `(tmux_session, tmux_window, tmux_pane)` 唯一。同一 tmux session 可绑定不同 pane；同一 cwd 仅在相同 backend 内禁止重复，避免 transcript 串线。

Codex 的 `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl` 路径不编码 cwd,backend 必须读取首行 `session_meta.payload.cwd` 和 binding `cwd` 比对。找不到 cwd 匹配的 rollout 时返回 `None`,不能兜底到全局最新文件,否则多 binding 会跨 chat tail 同一个 Codex 会话。

---

## 6. 部署

### Web control plane 独立进程

Web 进程与 Telegram/飞书 bridge 分开启动:

```bash
make install-dev
uv run tmuxbot web
# 等价: uv run python -m tmuxbot.web
```

启动流程为 `WebSettings.from_env()` → 控制面 SQLite migration → FastAPI/uvicorn；它不会读取 route、credential、Telegram polling 或飞书 WebSocket。SQLite 仅为旧 API 兼容而打开，Terminal Wall 页面不读取项目、Provider、通道或 TeamRun 数据；它通过 tmux Control Mode 提供宿主机 window inventory、pane snapshot、实时输出和原始键盘输入，默认监听 `127.0.0.1:13142`。

推荐统一运行 `tmuxbot serve --open`：Terminal Wall 常驻并监督独立 IM bridge child。`tmuxbot web` 适合开发或拆分部署。每张 Web 卡片打开一条 `tmux -CC` 控制连接，先发送 `capture-pane` 快照，再流式转发目标 pane 输出与浏览器原始键盘输入。

Terminal Wall 没有内置认证、ACL、IM route 或命令层。默认 loopback 监听只适用于本机访问；远程访问由外部反向代理/访问控制承担。浏览器的自由画布仅存 `localStorage`，不写控制面数据库。Terminal Wall 以单端使用为前提：浏览器卡片通过 Control Mode 将自身 cols/rows 提交给 tmux，真实 pane layout 与 xterm 同步重排，体验与 SSH/Tabby 一致。关闭 WebUI 后，下一次 SSH/Tabby attach 自然接管 tmux 尺寸；不要把同一 pane 同时作为不同尺寸的 Web 与 SSH 终端使用。

### 开发启动 (tmux session)

```bash
bash bin/restart.sh         # systemd 若运行 ~/.local/bin/tmuxbot，先强制重装当前 checkout 的 uv tool，再重启并验证；fallback runner 含 3 次重试
bash bin/status.sh          # 看进程 / session / 日志
bash bin/stop.sh            # 优雅停
```

### 生产部署 (systemd user service, 推荐)

推荐先用 Claude Code native installer 安装 `claude`,并在 `.env` 中配置绝对路径:

```bash
curl -fsSL https://claude.ai/install.sh | bash
echo "CLAUDE_BIN=$HOME/.local/bin/claude" >> .env
```

`CLAUDE_BIN` 在 `ensure_running()` 运行时读取,不会依赖 systemd/tmux 的非交互 shell `PATH`。这也避开 npm 全局安装缺少 native optional dependency 时的 `claude native binary not installed` 故障。`CODEX_BIN` 同理可配置 codex 绝对路径。

### Runtime V2 灰度与 Claude hooks

- `TMUXBOT_RUNTIME_V2=off`:兼容 reducer 发送。
- `TMUXBOT_RUNTIME_V2=shadow`:仍发送兼容结果,同时计算 V2 结果;差异日志只含事件类型、路由类型和长度区间,不记录消息正文。
- `TMUXBOT_RUNTIME_V2=on`:只发送 V2 reducer 结果。
- `TMUXBOT_CLAUDE_HOOKS=true`:启动时幂等合并 tmuxbot 自有 hooks 到 `~/.claude/settings.json`,保留其他 hook 与设置。hook 命令只把官方事件写入 `data/claude-hooks.jsonl`,由 Claude adapter 消费。

无论模式为何,执行面始终是 tmux pane 内的交互式 Claude/Codex/Pi CLI;hooks 与 JSONL 都只是观测源。

```bash
mkdir -p ~/.config/systemd/user
ln -sf "$(pwd)/deploy/systemd/tmuxbot.service" ~/.config/systemd/user/tmuxbot.service
systemctl --user daemon-reload
systemctl --user enable --now tmuxbot.service
loginctl enable-linger $USER   # 让 service 在 logout 后继续跑

# 或安装单一 service 并打开 tmux/provider watchdog
# tmuxbot install-service --now

# 日志 / 重启 / 停
journalctl --user -u tmuxbot -f
systemctl --user restart tmuxbot
systemctl --user stop tmuxbot
```

bot crash 后 5s 内自动拉起 (`Restart=always RestartSec=5s`)；unit 设置
`StartLimitIntervalSec=0`，连续瞬时故障不会触发永久 start-limit。内存上限
`MemoryHigh=2G MemoryMax=4G`,多 binding 后可适当调高。

### 单实例承载多 credential

每台主机只运行一个 `tmuxbot.service` / bridge supervisor。所有 Telegram Bot 和飞书 App
route 放在同一份 bindings 中，按 `(channel, bot_token_env)` 分组创建 frontend；adapter
仍按 route 解析。不要为第二个飞书 App 再复制 systemd unit、data dir 或 offsets。

`lark-oapi` 1.x 把 WebSocket event loop 保存在模块全局变量。tmuxbot 为每个飞书 App
独立加载 SDK WebSocket 模块和 worker loop，因此单进程可同时维持多条飞书连接；stop
时显式断开 websocket 并关闭该 loop。旧的 `tmuxbot-bridge-refresh@*.timer` 和 app-specific
rotation timer 应停用删除，frontend 自身负责断线退避重连。

多实例只用于显式的故障隔离/灰度，且必须使用完全不重叠的 route、bindings、offsets、
state、lock 和数据库；它不是多 credential 的默认部署方式。

### ensure_running — 按需重建 tmux + --resume

`ensure_running(binding)` 逻辑(每次收到消息都会调):

1. 检查 tmux session 是否存在,不存在则新建
2. 检查 pane 当前命令是否为对应 CLI,已在跑则跳过
3. Claude 使用 `${CLAUDE_BIN:-claude} --dangerously-skip-permissions --resume <session_id>` 重启(上下文不丢)
4. Codex 使用 `${CODEX_BIN:-codex} resume --dangerously-bypass-approvals-and-sandbox [-m <~/.codex/config.toml 的 model>] <session_id>` 重启(上下文不丢)；无历史会话的新启动同样读取该配置。配置缺失时不传 `-m`。

> `--resume` 不保留 `--dangerously-skip-permissions` 标志(上游 Issue #21974),所以每次都要重传。
> Codex 在受管会话重启后会应用当前 `~/.codex/config.toml` 的模型默认值；原生 `/model` 仍可用于当前运行会话的临时选择。

默认 `TMUXBOT_LIFECYCLE_ENABLED=1`、`TMUXBOT_LIFECYCLE_INTERVAL=3600`：bridge 每小时
只巡检已存在的 tmux pane。人工 `tmux kill-session -t <name>` 或 IM `/tmuxstop` 后，会话保持
关闭；巡检不会新建它，下一条消息进入共享 dispatch 时才调用 `ensure_running` 恢复。巡检与消息
入口共享锁，不会并发重复拉起。Web 控制台对应 `POST /api/managed-sessions/{id}/stop`：只关闭记录指向的 tmux，
不删除项目、受管记录或通道 binding；活动 TeamRun 会拒绝该操作。

Pi 另有独立的双层异常巡检。受管 `tmuxbot-session-handoff.ts` extension 将结构化 assistant
provider error 先写成 `recovering`，只有 Pi 发出 `agent_settled`、确认 retry/compaction/follow-up
都结束后，才升级为 `terminal_error` sidecar；bridge 复核精确 target/cwd/session/transcript 后
向原 endpoint 通知一次，重试过程中的 transcript error 不再即时误报。

第二层由 `TMUXBOT_PI_TERMINAL_HEALTH_ENABLED=1` 启用，默认
`TMUXBOT_PI_TERMINAL_HEALTH_INTERVAL=600`。它不重启、不中断任何 Pi：只有同一精确
session 连续 3 个周期显示真实 Pi working spinner、JSONL 大小及稳定屏幕均无进展，并且没有
retry、自动压缩、session handoff 或活跃子工具时，才去重提示人工使用 `/screen` 检查。正常
idle/working、自愈和不确定状态一律静默；持久 fingerprint 使 bridge 重启后不会重复通知。

### lifecycle health audit — 每小时只巡检，不休眠 provider

`lifecycle.py` 默认每 3600 秒巡检一次**已存在**的 route pane。巡检与入站消息共用
`State.ensure_locks[binding.name]`，调用 adapter 的 `ensure_running()` 重新验证前台 provider
与进程树；仅当 provider 明确不健康时才调用其受控恢复 seam。它不读取 `State.last_active`，
不向 Claude/Codex/Pi 发送退出命令，不按空闲时间回收任何 TUI，也不创建缺失 tmux target。

人工 `/tmuxstop` 或 `tmux kill-session` 后，缺失 session 会被健康巡检跳过；下一条精确 route
消息仍是唯一的按需恢复入口。这样定时任务和长任务持续保留，同时保有低频异常 pane 自愈能力。

---

## 7. 当前命令清单

### TG / 飞书共用命令 (经 `dispatch.py` 分发)

| 命令 | 行为 |
|---|---|
| `/esc` | 发 Escape 到 TUI(中断当前生成) |
| `/cc` | 发 C-c(取消/清空输入) |
| `/eof` | 发 C-d(退出 claude) |
| `/tmuxstop` | 关闭整个 tmux，保留 binding/历史；下一条消息按需恢复 |
| `/screen` | 抓 tmux 屏幕推回 |
| `/info` | 聚合统计(累计 token / 缓存命中率,只读 jsonl) |
| `/restart` | C-c + C-d + ensure_running |
| `/new` | 别名 → `/clear`,注入 TUI |
| `/rename` | 注入 TUI 进 pending_rename 态;下一条文本作名字 |
| `/context /cost /usage /compact /clear /stats /help` | TUI 透传 + capture_and_push 结构化反馈 |

### TG 专属命令 (BotFather 注册菜单)

| 命令 | 行为 |
|---|---|
| `/status` | 综合状态 4 章节: 🔌连接 / 📊上下文 / 💰用量 / 📈累计 + 🚦订阅配额 |
| `/whoami` | 我的 user_id / chat_id / thread_id(调试) |
| `/resume` | 注入到 TUI,picker 由 `detect_idle_picker` 自动推 inline keyboard |

---

## 8. 关键事实 (实测, 不能错)

参见 `CLAUDE.md` 第 2 节。摘要:

- `cwd` 编码:绝对路径里所有非 `[A-Za-z0-9]` 字符都替换为 `-`
- Claude/Codex 的 `paste-buffer -p` 前先等 TUI idle；paste 渲染后及每次 Enter 前再要求连续稳定 idle 0.5s，封住“初检 idle → 队列/重绘转 busy → Enter 被忽略”的竞态。Pi 是显式例外：官方 TUI 在 streaming/Working 时支持普通 Enter 将消息作为 steering queue 提交，所以 `ProviderCapabilities.accepts_input_while_busy=True`，入站文字/附件以及 `/rename` pending 后的名字值都会立即 paste + Enter，不等待 300s idle；此路径不能以“仍 busy”作为成功，必须观察 composer 清空/变化，避免假确认。`/new`、`/compact` 等 slash/picker/control command 不能误入 steering queue；Pi 非 `IDLE` 时共享 dispatch 必须立即在原 endpoint 回复“未执行”并提示先 `/esc`，不得让 IM handler 无声等待 300 秒后才抛 `TmuxBusyTimeout`。provider CLI 从 shell 唤醒是另一条显式路径：只按 foreground allowlist 判断，不得让旧 TUI scrollback 的 `Working...` 阻止启动；启动后必须验证真实 provider footer/status 出现。未知 foreground、foreground revalidation 失败或 TUI 未 ready 必须抛错到 dispatch，不能静默返回后继续把用户消息粘进 shell。所有 provider 的原草稿仍在时才有限重试，避免漏交与重复提交。
- claude TUI 事务式 flush jsonl → AskUserQuestion 被全局宪法封禁
- TG 4096 限 UTF-16 单位。最终 assistant 回复的每个分片必须取得 Bot API 返回的真实 `message_id` 才算送达；返回 `None`/抛错时 JSONL tailer 不提交当前行 offset，而是下一轮重试，并在成功时记录 message IDs。tailer 首次发现 transcript 时，已有持久 provider identity 的 bridge bootstrap 才跳到 EOF 防历史回吐；新 provision 且 identity 仍为空的 route 必须从 0 读取，否则 Pi 首个 turn 在轮询前完整落盘时会静默丢掉第一条回复。
- `setMessageReaction` 需 Bot API 7.0+ (aiogram 3.13+)
- `sendChatAction("typing")` 每 4s 刷一次维持 ~5s 显示
- `/compact` 完成硬信号: `type=system, subtype=compact_boundary` + `compactMetadata.preTokens/postTokens`
- tailer 积压保护: 单次落盘超 512KB 判定为事务式 flush 爆发 → 跳末尾,不回吐
- 飞书无 typing API; 飞书 text 消息不能编辑,必须用 interactive card
- `ReplyDocument` 的 fenced code 支持语言和 `filename=...`; Markdown pipe table 在飞书映射为 Card 2.0 根级 `table`，Telegram HTML 路径安全退化为对齐 `<pre>`（普通 Bot API HTML 不支持原生 table）
- Pi `/new` 的 JSONL 是延迟持久化：TUI 先显示 `✓ New session started`，新文件要等首条 assistant 回复才落盘。命令确认使用 TUI marker，但 `pending_session_handoff_after` 必须一直保留到 tailer 认领新 JSONL，不能因即时看不到文件而清除。
- Pi 命令分三类维护：文本 capture（`/session`）、JSONL 硬信号（`/new`、`/clone`、`/compact`）和 interactive picker（`/resume`、`/tree`、`/fork`、`/settings`、`/model`、`/scoped-models`、`/trust`、`/import`）。Pi picker 只做屏幕硬信号识别：导航/提交/取消控制提示必须紧邻当前实时 footer，命中后仅向原 endpoint 通知 SSH attach 到精确 pane；不得从 IM 模拟方向键、Enter、Escape、批准或取消，旧 Telegram/飞书卡 callback 也必须拒绝。interactive session switch 的事务保留到用户在 SSH 中完成选择，再调用 Pi 原生 `/session` 读取 File/ID 同步 route identity；不得 capture 后自动 Escape 关闭 picker。
- Pi `/compact` 与自动压缩的完成硬信号都是同一 JSONL 新增 `type=compaction` entry，不是 Claude 的 `compact_boundary`。heartbeat 从 TUI `Compacting context...` / `Auto-compacting...` 建立可编辑 IM 状态卡，ETA 使用当前 session 最近 5 次 compaction 的中位耗时（无历史默认 180s），每约 12s 更新；entry 落盘后编辑为完成回执并带 `tokensBefore`/summary usage/Todo。若 TUI 离开 compacting 但无 entry，必须标记“未确认完成/可能未续跑”，不能假报成功。
- Pi 可由 extension 替换原生 footer。当前 parser 同时支持原生 footer 与 `pi-statusline` powerline（provider/model/thinking/cwd/Git/context/tokens/cache/cost/extension status）；composer 识别也必须接受该 statusline，Todo overlay 出现在编辑器上方时不能误入草稿。
- `@narumitw/pi-plan-mode` 的权威状态来自当前精确 transcript branch 最后一条 `type=custom, customType=plan-mode-state`，不能按 mtime 跨 session 猜。IM 映射 `plan active/ready/saved/implementing` 四态，页面内容底部显示对应中文 Plan widget；配套 `pi-statusline` 会把 ready 压缩为 `📝 plan ✓`，screen 与 JSONL enrichment 必须去重。`plan_mode_complete` toolResult 和 `custom_message/proposed-plan` 统一转为 `PLAN_UPDATE` 可编辑计划卡；bridge restart 只从 snapshot 恢复状态栏/widget，不重复发送历史计划卡。`/plan`、`/plan tools` 是交互菜单，其余 `/plan` 子命令和 inline prompt 直接 passthrough 并返回 TUI 回执；pi-tui-kit 的 `↑/↓ navigate • enter select • esc close` 等控制栏只有在紧邻实时 Pi footer 时才算当前交互，命中后 Telegram/飞书仅发送精确 SSH/tmux 提示，不生成选择或方向键控制卡；所有 `/plan` 入口在 Pi 非 IDLE 时立即拒绝，禁止进入 steering queue 延迟改变工具/模式，bridge 不重实现扩展状态机。
- `rpiv-todo` 的权威状态不是独立文件，而是当前 Pi JSONL 分支上最后一条合法 `toolResult(toolName="todo")` 的 `message.details.tasks` 完整快照。`read_tasks()` 必须沿最后 leaf 的 `parentId` 链 replay、忽略 abandoned branch 与 `deleted` 项。只要快照里仍有非 deleted task，Pi 的每条 assistant/working IM 消息都固定追加 TUI 风格的完整 `Todos (completed/total)` 面板，保留原顺序、`○/◐/✓`、task ID、`activeForm` 和 `blockedBy`；即使全部 completed 也继续显示，只有 clear/全部 deleted 后隐藏。Claude harness 继续使用原有 summary footer 语义。

---

## 9. 部署红线

- ❌ 不能 root/sudo 跑 claude
- ❌ 不要依赖 systemd/tmux 的 shell PATH 找 `claude`;生产环境配置 `CLAUDE_BIN` 绝对路径
- ❌ 不推荐用 npm 全局安装 Claude Code;若 npm optional dependency/postinstall 坏了会出现 `claude native binary not installed`
- ❌ 项目里不要配 `PreToolUse` hook
- ❌ `tmux_send_text` 不前置 Escape(中断要用 `/esc`)
- ❌ pkill 用 -TERM 杀不死 zombie(jsonl_poll_loop 不响应 SIGTERM) → 用 -KILL
- ✅ `~/.claude/settings.json` 必含 `"skipDangerousModePermissionPrompt": true`
- ✅ TG bot 在群里设管理员或 BotFather 关 privacy mode
- ✅ ACL **双重门禁** (TG 和飞书均适用): 用户白名单 + source 必须配置,未配置的 source 一律静默
- ✅ `.gitignore` 必含 `.env` / `bindings*.yaml` / `data*/`

---

## 10. 调试

```bash
# tmux sessions 状态
tmux list-sessions
tmux capture-pane -t claude-main:0.0 -p -S -50

# 看当前 jsonl
ls -t ~/.claude/projects/-home-you-projects-alpha/*.jsonl | head -1
tail -3 .../*.jsonl | python3 -m json.tool

# bot 日志
tail -f data/tmuxbot.log
grep "starting\|heartbeat\|polling\|EXCEPTION\|WARNING" data/tmuxbot.log
```

---

## 11. Milestone 路线

- **M1** (✅ 2026-05-27): 单文件骨架 + 双 binding + 命令组 + heartbeat typing + 消息反应 + picker 兜底
- **M2** (✅ 2026-05-27): 地毯代码审查 → 可插拔重构 (`backends/` + `frontends/` + `dispatch.py`)
- **M3** (✅ 2026-05-27): 接入 Codex CLI + 双 bot 共存 + systemd 部署
- **M4** (✅ 2026-05-29): 接入飞书前端 (lark-oapi WebSocket + interactive card) + 多实例

---

## 12. 版本与发布

- 当前版本源: `pyproject.toml` 的 `[project].version` 与
  `tmuxbot/__init__.py` 的 `__version__`
- 版本一致性由 `tests/test_project_metadata.py` 检查
- 版本策略见 `VERSIONING.md`
- 发布检查清单见 `RELEASE.md`
- 每个用户可见或运维相关变更先写入 `CHANGELOG.md` 的 `Unreleased`

发布前至少运行:

```bash
make check
make check-web
make version
make release-check   # 发布机运行：额外检查本机 tmux/CLI/运行目录
```
