# tmuxbot — 开发文档

> Telegram + 飞书 ↔ tmux AI CLI (Claude Code / Codex) 双向桥。可插拔多前端/多后端架构。
> 决策依据见 `RESEARCH.md`, 代码审查见 `CODE_REVIEW.md`, 变更历史见 `CHANGELOG.md`, 项目宪法见 `CLAUDE.md`。

---

## 1. 目标

让 Boss 在 Telegram 任意端点 (DM / 群 / forum topic) 发消息 → 注入对应 tmux pane 内的 claude → claude 输出实时回推同端点。
N 个 TG 端点 ↔ N 个 tmux session ↔ N 个 cwd, 互不串扰。**bot 只搬键盘 + 屏幕**, 不调 Claude API, 不消费 token。

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
│       └── tmuxbot.service    ← systemd user unit (Restart=always, MemoryMax=4G)
├── tmuxbot/                   ← Python package
│   ├── __init__.py
│   ├── __main__.py            ← 装配入口: backends + frontends + tailer/heartbeat
│   ├── state.py               ← Binding + State + fire()
│   ├── config.py              ← .env + bindings.yaml + offsets.json → State
│   ├── utils.py               ← encode_cwd / cwidth / render_table / offsets debounced
│   ├── tmux.py                ← tmux_send_text (async) / send_key / capture / pane_command
│   ├── picker.py              ← PICKER_BOTTOMBAR_RE / detect_idle_picker
│   ├── jsonl.py               ← jsonl_poll_loop + on_tmux_event (含 tool_aggregator + 积压保护)
│   ├── heartbeat.py           ← heartbeat_typing_loop (TUI 指纹判活跃)
│   ├── commands.py            ← capture_and_push (slash 注入 + 屏幕等待 + 结构化反馈)
│   ├── dispatch.py            ← 共享命令分发层 (TG/飞书共用 stop/capture/text 逻辑)
│   ├── quota.py               ← OAuth API 订阅配额 (5h/7d 五窗口 + 重置倒计时)
│   ├── backends/
│   │   ├── base.py            ← Backend ABC + CmdOpts
│   │   ├── claude_code.py     ← ClaudeCodeBackend: parse_event / parse_* / find_active_jsonl
│   │   │                         / ensure_running / find_tui_activity_fp / aggregate_usage
│   │   └── codex.py           ← CodexBackend
│   └── frontends/
│       ├── base.py            ← Frontend ABC (send_html/edit_html/send_pre/send_chat_action)
│       ├── telegram.py        ← TelegramFrontend: aiogram + ACL + ack middleware + handlers
│       └── feishu.py          ← FeishuFrontend: lark-oapi WebSocket + interactive card
├── bindings.yaml              ← 绑定配置 (gitignored)
├── .env                       ← TG_BOT_TOKEN / TG_CODEX_BOT_TOKEN / BOSS_USER_ID 等 (gitignored)
├── .env.example
├── .gitignore
├── pyproject.toml             ← aiogram>=3.13, pyyaml>=6.0, python-dotenv>=1.0; lark-oapi>=1.4 optional
├── data/                      ← gitignored
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
    │  paste-buffer inject → TUI idle 轮询 → Enter       │
    │  jsonl tailer → parse_event → aggregator → 推前端  │
    └───────────────────────────────────────────────────┘
```

**架构原则 (Boss 铁律)**:1 bot ↔ 1 backend ↔ N 个 tmux 子线程。不同 CLI 类型用不同 bot token,避免协议串扰。

### `TOKEN_TO_BACKEND` 映射 (`__main__.py`)

```python
TOKEN_TO_BACKEND = {
    "TG_BOT_TOKEN":       "claude_code",
    "TG_CODEX_BOT_TOKEN": "codex",
}
```

启动时校验每组 binding 的 `backend` 字段与 token 推断一致,不一致强制对齐 + WARNING。

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

所有消息以 **interactive card** 形式发送(设 `update_multi=True`),支持 PATCH 就地编辑——与 TelegramFrontend 的 `edit_message_text` 对等,工具调用聚合器可复用。

### typing 状态

飞书无对等 API,`send_chat_action` 为 no-op。heartbeat_typing_loop 仍然调用,但无视觉效果。

### ACL

- `open_id` 在 `FEISHU_BOSS_OPEN_IDS` 白名单(逗号分隔)
- `chat_id` 在本前端的 bindings 子集中
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
    backend: claude_code           # claude_code / codex
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
    thread_id: null                # 飞书不分 topic, 恒为 null
    bot_token_env: FEISHU          # 读 FEISHU_APP_ID / FEISHU_APP_SECRET
    backend: claude_code
    tmux_session: "claude-gamma"
    tmux_window: 0
    tmux_pane: 0
    cwd: /home/you/projects/gamma
```

**对应 `.env` 飞书相关变量**:

```bash
# bot_token_env=FEISHU → 读这三个变量
FEISHU_APP_ID=cli_xxxxxxxxxxxxxxxx
FEISHU_APP_SECRET=xxxxxxxxxxxxxxxxxxxxxxxxxxxx
FEISHU_BOSS_OPEN_IDS=ou_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx  # 逗号分隔多个

# 可选: 群消息是否仅 @bot 才响应 (默认 false,即不需要 @)
# FEISHU_GROUP_MENTION_ONLY=false
```

**校验红线**: `(chat_id, thread_id)` 全局唯一; `cwd` 全局唯一 (同 cwd 起两个 claude 会撞 jsonl); `tmux_session` 全局唯一。

---

## 6. 部署

### 开发启动 (tmux session)

```bash
bash bin/restart.sh         # 含 3 次重试, 启动后验证
bash bin/status.sh          # 看进程 / session / 日志
bash bin/stop.sh            # 优雅停
```

### 生产部署 (systemd user service, 推荐)

```bash
mkdir -p ~/.config/systemd/user
ln -sf "$(pwd)/deploy/systemd/tmuxbot.service" ~/.config/systemd/user/tmuxbot.service
systemctl --user daemon-reload
systemctl --user enable --now tmuxbot.service
loginctl enable-linger $USER   # 让 service 在 logout 后继续跑

# 日志 / 重启 / 停
journalctl --user -u tmuxbot -f
systemctl --user restart tmuxbot
systemctl --user stop tmuxbot
```

bot crash 后 5s 内自动拉起 (`Restart=always RestartSec=5s`)。内存上限 `MemoryHigh=2G MemoryMax=4G`,多 binding 后可适当调高。

### 多实例部署 (多飞书 app)

为每个飞书 app 创建独立 systemd unit,通过 `Environment=` 覆盖路径:

```ini
# ~/.config/systemd/user/tmuxbot-feishu2.service
[Service]
WorkingDirectory=%h/claude-project/tmuxbot
ExecStart=/usr/bin/python3 tmuxbot.py
Environment=TMUXBOT_DATA_DIR=%h/.tmuxbot/feishu2
Environment=TMUXBOT_BINDINGS=%h/.tmuxbot/feishu2/bindings.yaml
Environment=TMUXBOT_ENV=%h/.tmuxbot/feishu2/.env
Restart=always
RestartSec=5s
```

### ensure_running — 按需重建 tmux + --resume

`ensure_running(binding)` 逻辑(每次收到消息都会调):

1. 检查 tmux session 是否存在,不存在则新建
2. 检查 pane 当前命令是否为 claude,已在跑则跳过
3. `claude --dangerously-skip-permissions --model 'claude-opus-4-8[1m]' --resume <session_id>` 重启(上下文不丢)

> `--resume` 不保留 `--dangerously-skip-permissions` 标志(上游 Issue #21974),所以每次都要重传。

---

## 7. 当前命令清单

### TG / 飞书共用命令 (经 `dispatch.py` 分发)

| 命令 | 行为 |
|---|---|
| `/esc` | 发 Escape 到 TUI(中断当前生成) |
| `/cc` | 发 C-c(取消/清空输入) |
| `/eof` | 发 C-d(退出 claude) |
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

- `cwd` 编码: `/` 和 `.` 都替换为 `-`
- `paste-buffer -p` 后必须等 TUI idle 再 send Enter(idle 轮询, 超时 10s 强发)
- claude TUI 事务式 flush jsonl → AskUserQuestion 被全局宪法封禁
- TG 4096 限 UTF-16 单位
- `setMessageReaction` 需 Bot API 7.0+ (aiogram 3.13+)
- `sendChatAction("typing")` 每 4s 刷一次维持 ~5s 显示
- `/compact` 完成硬信号: `type=system, subtype=compact_boundary` + `compactMetadata.preTokens/postTokens`
- tailer 积压保护: 单次落盘超 512KB 判定为事务式 flush 爆发 → 跳末尾,不回吐
- 飞书无 typing API; 飞书 text 消息不能编辑,必须用 interactive card

---

## 9. 部署红线

- ❌ 不能 root/sudo 跑 claude
- ❌ 项目里不要配 `PreToolUse` hook
- ❌ `tmux_send_text` 不前置 Escape(中断要用 `/esc`)
- ❌ pkill 用 -TERM 杀不死 zombie(jsonl_poll_loop 不响应 SIGTERM) → 用 -KILL
- ✅ `~/.claude/settings.json` 必含 `"skipDangerousModePermissionPrompt": true`
- ✅ TG bot 在群里设管理员或 BotFather 关 privacy mode
- ✅ ACL **双重门禁** (TG 和飞书均适用): 用户白名单 + source 必须配置,未配置的 source 一律静默
- ✅ `.gitignore` 必含 `.env` / `bindings.yaml` / `data/`

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

## 4. `bindings.yaml` schema

```yaml
bindings:
  - name: tmuxbot-dev
    chat_id: <YOUR_USER_ID>          # DM: 正数 = user_id (留 0 触发首次 setup 自动锁定)
    thread_id: null                  # DM 无
    tmux_session: "claude-main"
    tmux_window: 0
    tmux_pane: 0
    cwd: /home/you/claude-project/some-project

  - name: forum-topic-example
    chat_id: -100<SUPERGROUP_ID>     # supergroup: t.me/c/X/Y 格式 加 -100 前缀
    thread_id: <TOPIC_ID>            # forum topic id
    tmux_session: "another-session"
    tmux_window: 0
    tmux_pane: 0
    cwd: /home/you/claude-project/another-project
```

**校验红线**: `(chat_id, thread_id)` 全局唯一; `cwd` 全局唯一 (同 cwd 起两个 claude 会撞 jsonl); `tmux_session` 全局唯一。

---

## 5. 当前命令清单

### TG 端命令 (BotFather 注册菜单)

| 命令 | 行为 |
|---|---|
| `/status` | 综合状态 4 章节: 🔌连接 / 📊上下文 / 💰用量 / 📈累计 (走 `parse_context` + `parse_cost` + `aggregate_jsonl_usage`) |
| `/info` | 累计 token + 缓存命中率 (只读 jsonl, 不进 tmux) |
| `/whoami` | 我的 user_id / chat_id / thread_id (调试) |
| `/new` | bot 端别名: 注入 `/clear` 到 TUI, 反馈"新会话" |
| `/resume` | 注入到 TUI, picker 由 `detect_idle_picker` 自动推 inline keyboard |
| `/rename` | 注入到 TUI, bot 进 `pending_rename` 态; 下一条 TG 文本作为名字注入 |
| `/esc` | 发 Escape 到 TUI (中断当前生成) |
| `/cc` | 发 C-c (取消/清空输入) |
| `/eof` | 发 C-d (退出 claude) |
| `/screen` | 抓 tmux 屏幕推回 (用 `<pre>` 包) |
| `/restart` | C-c + 重启 claude |

### TUI 透传命令 (bot 不拦, 直接 paste 进 tmux)

`/context`, `/cost`, `/usage`, `/stats`, `/help`, `/compact`, `/clear` — 走 `COMMAND_OPTS` 的 `capture_and_push` 兜底, 用专门 parser 出结构化反馈。

---

## 6. 关键事实 (实测, 不能错)

参见 `CLAUDE.md` 第 2 节 (避免重复)。摘要:

- `cwd` 编码: `/` 和 `.` 都替换为 `-`
- `paste-buffer -p` 后必须 sleep 0.5s 才能 send Enter
- claude TUI 事务式 flush jsonl → AskUserQuestion 被全局宪法封禁
- TG 4096 限 UTF-16 单位
- `setMessageReaction` 需 Bot API 7.0+ (aiogram 3.13+)
- `sendChatAction("typing")` 每 4s 刷一次维持 ~5s 显示

---

## 7. 部署红线

- ❌ 不能 root/sudo 跑 claude
- ❌ 项目里不要配 `PreToolUse` hook
- ❌ `tmux_send_text` 不前置 Escape (中断要用 `/esc`)
- ❌ pkill 用 -TERM 杀不死 zombie (jsonl_poll_loop 不响应 SIGTERM) → 用 -KILL
- ✅ `~/.claude/settings.json` 必含 `"skipDangerousModePermissionPrompt": true`
- ✅ TG bot 在群里设管理员或 BotFather 关 privacy mode
- ✅ ACL **双重门禁**: `from_user.id == BOSS_USER_ID` **且** `source_key (chat_id, thread_id)` 已在 bindings.yaml 配置。未配置的 source 一律静默 (不打 👀 / 不 typing / 不回复 / 不警告), 即使 Boss 本人发。两套 bot (claude / codex) 共用
- ✅ `.gitignore` 必含 `.env` / `bindings.yaml` / `data/`

---

## 8. 调试

```bash
# tmux sessions 状态
tmux list-sessions
tmux capture-pane -t claude主会话:0.0 -p -S -50

# 看当前 jsonl
ls -t ~/.claude/projects/-home-pyadmin-claude-project-tmuxbot/*.jsonl | head -1
tail -3 .../*.jsonl | python3 -m json.tool

# bot 日志
tail -f data/tmuxbot.log
grep "starting\|heartbeat\|polling\|EXCEPTION\|WARNING" data/tmuxbot.log
```

---

## 9. Milestone 路线

- **M1** (✅ 2026-05-27): 单文件骨架 + 双 binding + 命令组 + heartbeat typing + 消息反应 + picker 兜底
- **M2** (🚧 当前): 地毯代码审查 → 可插拔重构 (`core/` + `backends/` + `frontends/`)
- **M3** (⏳): 接入 codex cli (`codex-*` tmux session, 沿用 binding 模型)
- **M4** (⏳): 接入飞书前端 (lark-oapi)
