# tmuxbot — 开发文档

> Telegram + 飞书 ↔ tmux AI CLI (Claude Code / Codex / Oh My Pi) 双向桥。精确话题路由 + 可插拔 adapter 架构。
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
│   ├── control_plane/         ← SQLite migration/repository + tmux inventory
│   ├── providers/             ← CLI discovery、能力与统一启动参数
│   ├── runtime/               ← 串行 tmux runtime/input queue + OMP identity/plan/health helpers
│   ├── teamrun/               ← DAG、worker、worktree、mailbox、artifact、scheduler
│   ├── web/                   ← FastAPI、认证、setup、terminal 与静态 WebUI
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
│   │   └── omp.py             ← OmpBackend: OMP v3 JSONL、原生 footer 弱信号、exact resume/sidecar
│   └── frontends/
│       ├── base.py            ← Frontend ABC 与回复发送契约
│       ├── telegram.py        ← TelegramFrontend: aiogram + ACL + handlers
│       ├── feishu.py          ← FeishuFrontend: lark-oapi WebSocket + Card JSON 2.0
│       └── feishu_cards.py    ← 飞书卡片构建与分页
├── omp-extensions/             ← 每次受管 OMP 启动显式加载的 identity/health extension
├── webui/                     ← React/Vite/xterm.js 中文控制台源码
├── bindings.yaml              ← 绑定配置 (gitignored; 多实例 bindings*.yaml 也忽略)
├── .env                       ← TG_BOT_TOKEN / TG_CODEX_BOT_TOKEN / TG_OMP_BOT_TOKEN / BOSS_USER_ID 等 (gitignored)
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
              │            dispatch_incoming_text           │
              │  route command policy / 普通文本 / 附件      │
              └─────────────────────┬──────────────────────┘
                                    │ backend_for(binding)
           ┌────────────────────────┼────────────────────────┐
           │                        │                        │
┌──────────▼──────────┐  ┌──────────▼──────────┐  ┌────────▼─────────┐
│ ClaudeCodeBackend   │  │ CodexBackend        │  │ OmpBackend       │
│ hooks/JSONL/TUI     │  │ rollout JSONL/TUI   │  │ v3 JSONL/sidecar │
│ provider lifecycle │  │ provider lifecycle  │  │ registry launch  │
└──────────┬──────────┘  └──────────┬──────────┘  └────────┬─────────┘
           └────────────────────────┼────────────────────────┘
                                    ▼
             tmux pane (各 binding 独立的真实交互式 TUI)
             transcript tailer → ProviderEvent → 同 endpoint

```

**架构原则**:frontend 先按 `(channel, credential, chat_id, thread_id)` 命中 route，再以 `frontend.backend_for(binding)` 选择 Claude/Codex/OMP adapter。credential 只划分 Bot/App 身份，不决定 CLI 类型。群根与未绑定 topic/thread 完全静默；新增 topic route 通过 YAML、`tmuxbot route bind` 或 Admin DM 显式创建，不由群内 `/init` 隐式开通。

完整设计、配置和兼容迁移见 [`docs/topic-routing.md`](docs/topic-routing.md)。Boss 在 Admin DM 中用自然语言创建/绑定 tmux 与 Telegram/飞书话题的模板和验收流程见 [`docs/admin-dm-operations.md`](docs/admin-dm-operations.md)。低层配置操作仍使用 `tmuxbot route list|inspect|validate|bind|unbind`；普通 Admin LLM 的项目开通只使用 `tmuxbot admin provision-project`：一个 topic intent（新建标题、Telegram topic URL、或精确 chat/thread）加 route name/cwd/backend，即可获得固定 plan → apply → verify 流程；tmux 默认 `NAME:0.0`，不存在时事务创建、存在时只复用 exact-cwd pane。`inventory|telegram-topic|feishu-topics|create-topic|bind-topic|move-topic|adopt-omp-session|verify` 保留为低层诊断、恢复和迁移接口。若操作者在 tmux/SSH 中直接切换 OMP 会话，route identity 未及时同步导致回推停止，必须对精确的绝对 JSONL 路径执行 `adopt-omp-session` plan，再带 `--apply` 原子认领已校验的同 cwd OMP 会话；该命令不按 mtime 猜测，会重启 bridge 并验证 route/tmux/service。Telegram route 只需要 `chat_id + thread_id`，`https://t.me/c/CHAT/THREAD` 已足够，不能额外要求 message ID 或 `thread_root_message_id`。直接编辑 YAML 仍作为离线恢复能力保留。

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
    backend: claude_code           # claude_code / codex / omp
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

启动流程为 `load_config()` → `WebSettings.from_env()` → SQLite migration →
FastAPI/uvicorn; 它不会创建 Telegram polling 或飞书 WebSocket。当前进程提供
中文 WebUI、认证后的项目/provider/managed-session/channel/TeamRun API、只读 tmux
inventory，以及显式授权后的终端接管。默认只监听 `127.0.0.1:8765`。

推荐统一运行 `tmuxbot serve --open`：WebUI 常驻并监督独立 bridge child；缺少 IM
配置时 WebUI 仍可完成首次设置。纯 `tmuxbot web` 适合开发或拆分部署。

首次启动的底层 API 顺序如下（浏览器 WebUI 会自动完成同一流程）:

1. 保持 listener 为 loopback,不要启动反向代理。
2. `tmuxbot serve --open` 会生成短时一次性本机 setup grant；固定部署也可运行
   `openssl rand -hex 32`，把输出写入 `.env` 的 `TMUXBOT_WEB_SETUP_TOKEN`。
3. 启动 Web 进程。若不用浏览器，可先 GET status 取得 bootstrap CSRF cookie/token,
   再携带 `X-CSRF-Token` 与 `X-Setup-Token` POST setup:

```bash
curl -sS -c /tmp/tmuxbot-web.cookies \
  http://127.0.0.1:8765/api/auth/status
export CSRF_TOKEN='<csrf_token from the previous response>'
export SETUP_TOKEN='<TMUXBOT_WEB_SETUP_TOKEN from the local .env>'
curl -sS -b /tmp/tmuxbot-web.cookies -c /tmp/tmuxbot-web.cookies \
  -H "X-CSRF-Token: ${CSRF_TOKEN}" \
  -H "X-Setup-Token: ${SETUP_TOKEN}" \
  -H 'Content-Type: application/json' \
  --data '{"password":"replace-with-a-strong-password"}' \
  http://127.0.0.1:8765/api/auth/setup
```

4. setup 成功后删除 `TMUXBOT_WEB_SETUP_TOKEN`,重启 Web 进程。
5. 最后设置 secure cookie/public origin 并启用 TLS 反向代理。

浏览器或未来 UI 也必须通过 header 提交一次性 setup secret。直接 peer 必须为
loopback,且 setup secret 必须常量时间匹配;`X-Forwarded-For` 不参与授权。status
返回的 bootstrap CSRF 不是授权,响应不会返回 setup secret。

**禁止将该端口直接暴露到公网。** 需要远程访问时,使用 TLS 反向代理,
设置 `TMUXBOT_WEB_SECURE_COOKIE=true` 和精确的 `TMUXBOT_WEB_PUBLIC_ORIGIN`。
`deploy/systemd/tmuxbot-web.service` 通过 `EnvironmentFile` 读取配置与密钥,
不在 `ExecStart` 中传递密码或 secret。根据实际安装位置修改 unit 内三处路径。

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
`OMP_BIN` 同样可固定 Oh My Pi 可执行文件；未配置时 registry 按 `PATH`、`~/.local/bin/omp` 解析。OMP 的显示名、route backend、默认 Telegram credential 与启动 argv 全部由 provider registry 提供，Web/API 不接受浏览器提交 binary path、tmux target 或自定义 argv。

### Runtime V2 灰度与 Claude hooks

- `TMUXBOT_RUNTIME_V2=off`:兼容 reducer 发送。
- `TMUXBOT_RUNTIME_V2=shadow`:仍发送兼容结果,同时计算 V2 结果;差异日志只含事件类型、路由类型和长度区间,不记录消息正文。
- `TMUXBOT_RUNTIME_V2=on`:只发送 V2 reducer 结果。
- `TMUXBOT_CLAUDE_HOOKS=true`:启动时幂等合并 tmuxbot 自有 hooks 到 `~/.claude/settings.json`,保留其他 hook 与设置。hook 命令只把官方事件写入 `data/claude-hooks.jsonl`,由 Claude adapter 消费。

无论模式为何,执行面始终是 tmux pane 内的交互式 Claude/Codex/OMP CLI;hooks、sidecar 与 JSONL 都只是观测/身份来源，不进入 RPC/SDK/headless 执行链。

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

### ensure_running — 按需重建 tmux + provider 精确恢复

`ensure_running(binding)` 逻辑(每次收到消息都会调):

1. 检查 tmux session 是否存在,不存在则新建；
2. 检查 pane 当前命令和进程树是否为 route 对应 CLI；
3. Claude 使用 `${CLAUDE_BIN:-claude} --dangerously-skip-permissions --resume <session_id>` 重启(上下文不丢)；
4. Codex 使用 `${CODEX_BIN:-codex} resume --dangerously-bypass-approvals-and-sandbox [-m <~/.codex/config.toml 的 model>] <session_id>` 重启；
5. OMP 只从 provider registry 取得 `${OMP_BIN:-omp} --approval-mode yolo --extension <managed-extension-absolute-path>`。route 有 transcript pin 时仅追加 `--resume <exact-absolute-jsonl-path>`，绝不使用 `--continue`、`--session`、`--approve`、RPC 或 print 参数。pin 的首个有效 `type:"session"` header 必须有受支持 version、非空 ID、canonical cwd 精确匹配；失败时保留 pin 并拒绝静默新建会话。

> `--resume` 不保留 `--dangerously-skip-permissions` 标志(上游 Issue #21974),所以每次都要重传。
> Codex 在受管会话重启后会应用当前 `~/.codex/config.toml` 的模型默认值；原生 `/model` 仍可用于当前运行会话的临时选择。

默认 `TMUXBOT_LIFECYCLE_ENABLED=1`、`TMUXBOT_LIFECYCLE_INTERVAL=3600`：bridge 每小时
只巡检已存在的 tmux pane。人工 `tmux kill-session -t <name>` 或 IM `/tmuxstop` 后，会话保持
关闭；巡检不会新建它，下一条消息进入共享 dispatch 时才调用 `ensure_running` 恢复。巡检与消息
入口共享锁，不会并发重复拉起。Web 控制台对应 `POST /api/managed-sessions/{id}/stop`：只关闭记录指向的 tmux，
不删除项目、受管记录或通道 binding；活动 TeamRun 会拒绝该操作。

OMP 使用受管 `omp-extensions/tmuxbot-session-handoff.ts` 提供两套 versioned sidecar。identity 写入 `omp-session-handoffs/`，health 写入 `omp-session-health/`；文件名按 exact tmux target 派生。handoff 必须匹配 `tmuxTarget/cwd/sessionId/transcriptPath/processId`，其中 `processId` 必须属于该 pane 的 live OMP process。新 session 的 JSONL 会延迟到首条消息才创建，因此文件尚不存在时只接受官方 sessions root 与匹配 session ID 的 provider-authored pending path；文件出现后必须通过 transcript header/cwd 校验。已有 `omp` 进程但缺少有效 identity sidecar 时 fail closed，提示 `/restart`，禁止按 cwd/mtime 猜测会话。

health extension 在 `agent_start` 写 `working`，非终止 `agent_end` 保持 `working/recovering`，只有 terminal end 才写 `idle` 或 `terminal_error`。bridge 仅在 exact target/cwd/session/transcript 全部一致时消费错误状态。第二层由 `TMUXBOT_OMP_TERMINAL_HEALTH_ENABLED=1` 启用，默认 `TMUXBOT_OMP_TERMINAL_HEALTH_INTERVAL=600`；它只对同一精确 session 的持续无进展状态去重告警，不重启或中断 OMP。

### lifecycle health audit — 每小时只巡检，不休眠 provider

`lifecycle.py` 默认每 3600 秒巡检一次**已存在**的 route pane。巡检与入站消息共用
`State.ensure_locks[binding.name]`，调用 adapter 的 `ensure_running()` 重新验证前台 provider
与进程树；仅当 provider 明确不健康时才调用其受控恢复 seam。它不读取 `State.last_active`，
不向 Claude/Codex/OMP 发送空闲退出命令，不按空闲时间回收任何 TUI，也不创建缺失 tmux target。

人工 `/tmuxstop` 或 `tmux kill-session` 后，缺失 session 会被健康巡检跳过；下一条精确 route
消息仍是唯一的按需恢复入口。这样定时任务和长任务持续保留，同时保有低频异常 pane 自愈能力。

---

## 7. 当前命令清单

### TG / 飞书共用命令 (经 `dispatch.py` 分发)

| 命令 | 行为 |
|---|---|
| `/esc /cc /eof` | Claude/Codex 可发送对应控制键；OMP 不接受 IM 远程按键，只返回 exact pane 的 SSH attach 提示 |
| `/tmuxstop` | 关闭整个 tmux，保留 binding/历史；下一条消息按需恢复 |
| `/screen` | 抓 tmux 屏幕推回 |
| `/info` | 聚合统计(累计 token / 缓存命中率,只读 JSONL) |
| `/restart` | Claude/Codex 沿原控制流程重启；OMP 使用 clean pane respawn 后按 exact transcript path 恢复，不向原生 TUI 注入 C-c/C-d |
| `/new /compact /clear /fresh` | OMP 控制命令只允许在 IDLE 时执行；busy 时立即明确拒绝，绝不进入 steering queue |
| `/plan` | OMP 本地帮助：只报告当前 `mode_change` 状态，未启用时提示 SSH attach 后用默认 `Alt+Shift+P`；不会把 `/plan` 注入 pane |
| OMP 原生菜单 | `/login /model /scoped-models /settings /statusline /resume /tree /trust /fork /import` 仅识别当前菜单并提示 SSH 操作，不从 IM 模拟导航、确认或取消 |

### TG 专属命令 (BotFather 注册菜单)

| 命令 | 行为 |
|---|---|
| `/status` | 综合状态 4 章节: 🔌连接 / 📊上下文 / 💰用量 / 📈累计 + 🚦订阅配额 |
| `/whoami` | 我的 user_id / chat_id / thread_id(调试) |
| `/resume` | Claude/Codex 依各自 adapter 处理；OMP 只提示通过 SSH 操作原生 picker，sidecar 在会话切换后同步 exact identity |

---

## 8. 关键事实 (实测, 不能错)

参见 `CLAUDE.md` 第 2 节。摘要:

- Claude 的 cwd session 目录使用编码路径；OMP 不扫描编码 cwd 或“最新会话”，只认 exact target-scoped handoff sidecar，其次才使用 binding 的 exact transcript pin。当前 OMP 会话通常位于 `~/.omp/agent/sessions/<encoded-project>/<timestamp>_<id>.jsonl`，但目录结构不是发现协议。
- Claude/Codex 的 `paste-buffer -p` 前先等 TUI idle。OMP 是显式例外：普通文字/附件在 Working 时可提交到原生 steering queue (`accepts_input_while_busy=True`)；`/new`、`/compact`、`/clear`、`/fresh` 及所有 picker/control command 必须要求 IDLE，busy 时立即回复未执行，不能排队等待或进入 steering。
- OMP 的 `/restart` 走 clean `respawn-pane`，再由 `ensure_running()` 使用 registry argv 和 exact `--resume <absolute path>`；禁止向 OMP TUI 注入 C-c/C-d 来猜测退出状态。
- OMP 的 IM 状态栏镜像当前原生 footer 的显示语义和顺序：`⬢ model (provider)`、`◕ effort`、`📁 cwd`、`⑂ branch`、`◫ percent/limit ⟲`、cost、session title、active loader。JSONL 的累计 input/output/cache 只用于 `/info` 等统计，不覆盖 TUI 显示名，也不进入紧凑状态栏。
- OMP JSONL 当前 canonical schema 是 v3：固定 title/session slot 之后，以最后 entry 的 `id/parentId` 回溯当前 branch，忽略 abandoned branch。runtime metadata 从 `model_change.model="provider/model"`、`thinking_level_change.thinkingLevel`、`title/title_change` 与 assistant `usage/cost` 读取；assistant `provider/model` 只作 fallback。
- OMP assistant `message.content` 的 `thinking/toolCall/text` 规范化为 tool progress/final text；只有 `write` toolCall 写入非空 `local://*-plan.md` 才产生 `PLAN_UPDATE`。plan active 状态只看当前 branch 最后的 `mode_change.mode="plan"`，不依赖第三方 plan extension。
- `/plan` 是 tmuxbot 本地帮助，不是 OMP slash command。它不会写入 transcript；未处于 plan mode 时仅提示 SSH attach 后使用默认 `Alt+Shift+P`，并注明自定义 keybindings 可能改变快捷键。
- OMP todo 的权威快照是当前 branch 最后一个成功 `toolResult(toolName="todo").details.phases`，或更新的 `custom(customType="user_todo_edit").data.phases`。phase 下 task 支持 `pending/in_progress/completed/abandoned/blocked` 与可选 `blocker`；渲染按 phase 分组，abandoned 不显示。
- OMP `/compact` 与自动压缩的完成硬信号是 canonical `type="compaction"`（或 extension lifecycle/session switch）；稳定元数据只有 `preTokens=tokensBefore`，无官方字段时 `postTokens/durationMs=None`。Claude 的 `compact_boundary` 契约保持不变。
- OMP 17.3.2 原生 `╭── π … ╮` / `╰─ … ─╯` footer 与紧邻 footer、以 `⟦esc⟧` 结尾的 braille loader 仅是版本化弱信号。模型/provider/usage 以 JSONL 为准；屏幕只补可明确解析的 effort、cwd、branch、context、cost、plan/session label。自定义 statusline 不属于 adapter 契约。
- OMP 原生 ask/approval/model/resume/plan review 等交互只有在控制提示紧邻当前 footer 时才视为 live；Telegram/飞书只发送 exact tmux target 的 SSH 提示，所有旧 callback 或 IM 按键动作 fail closed。
- TG 4096 限 UTF-16 单位。最终 assistant 回复的每个分片必须取得 Bot API 返回的真实 `message_id` 才算送达；失败时 JSONL tailer 不提交当前行 offset并在下一轮重试。新 provision 且 identity 仍为空的 route 必须从 0 读取，避免首个完整 turn 在轮询前落盘后丢失。
- `setMessageReaction` 需 Bot API 7.0+ (aiogram 3.13+)；`sendChatAction("typing")` 每 4s 刷一次维持约 5s 显示；飞书无 typing API，text 消息不可编辑，需使用 interactive card。
- tailer 积压保护: 单次落盘超 512KB 判定为事务式 flush 爆发 → 跳末尾,不回吐。
- `ReplyDocument` 的 fenced code 支持语言和 `filename=...`; Markdown pipe table 在飞书映射为 Card 2.0 根级 `table`，Telegram HTML 路径安全退化为对齐 `<pre>`。
- claude TUI 事务式 flush JSONL，AskUserQuestion 被全局宪法封禁。

---

## 9. 部署红线

- ❌ 不能 root/sudo 跑 claude
- ❌ 不要依赖 systemd/tmux 的 shell PATH 找 provider；生产环境可用 `CLAUDE_BIN` / `CODEX_BIN` / `OMP_BIN` 固定绝对路径，OMP 仍必须通过 registry 组合受管启动参数
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
