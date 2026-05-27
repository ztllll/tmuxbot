# tmuxbot — 开发文档

> Telegram ↔ tmux Claude Code TUI 双向桥。当前 `tmuxbot.py` 单文件 ~1911 行 (P2.5 已修🔴+🟡问题, M2 可插拔重构进行中)。
> 决策依据见 `RESEARCH.md`, 代码审查见 `CODE_REVIEW.md`, 项目宪法见 `CLAUDE.md`。

---

## 1. 目标

让 Boss 在 Telegram 任意端点 (DM / 群 / forum topic) 发消息 → 注入对应 tmux pane 内的 claude → claude 输出实时回推同端点。
N 个 TG 端点 ↔ N 个 tmux session ↔ N 个 cwd, 互不串扰。**bot 只搬键盘 + 屏幕**, 不调 Claude API, 不消费 token。

---

## 2. 当前文件结构 (P3 + P4 + bin/deploy 后)

```
tmuxbot/                       ← 仓库根
├── tmuxbot.py                 ← thin entry (~16 行) 调 tmuxbot package
├── bin/                       ← 运维脚本
│   ├── restart.sh             ← 重启 (含失败自动重试 3 次, 解决 first-attempt fail)
│   ├── stop.sh                ← 优雅停 (TERM → KILL)
│   └── status.sh              ← 看进程 / session / 日志
├── deploy/
│   └── systemd/
│       └── tmuxbot.service    ← systemd user unit (Restart=always, MemoryMax=4G)
├── tmuxbot/                   ← Python package
│   ├── __init__.py / __main__.py / state.py / utils.py / tmux.py
│   ├── config.py / picker.py / jsonl.py / heartbeat.py / commands.py
│   ├── backends/
│   │   ├── base.py            ← Backend ABC
│   │   ├── claude_code.py     ← ClaudeCodeBackend
│   │   └── codex.py           ← CodexBackend (P4)
│   └── frontends/
│       ├── base.py            ← Frontend ABC
│       └── telegram.py        ← TelegramFrontend (单 backend + 自有 bindings)
├── bindings.yaml              ← 绑定配置 (gitignored)
├── .env                       ← TG_BOT_TOKEN / TG_CODEX_BOT_TOKEN / BOSS_USER_ID (gitignored)
├── .env.example
├── .gitignore
├── pyproject.toml             ← aiogram>=3.13, pyyaml>=6.0, python-dotenv>=1.0
├── data/                      ← gitignored
│   ├── offsets.json           ← jsonl byte offset 持久化 (debounced 5s)
│   ├── tmuxbot.log
│   └── tmuxbot.lock
├── CLAUDE.md                  ← 项目宪法 + §9 决策日志
├── DEVELOPMENT.md             ← 本文件
├── CODE_REVIEW.md             ← P2 地毯审查
├── RESEARCH.md                ← 立项调研
├── README.md                  ← 入口
└── LICENSE                    ← MIT
```

## 双 bot 多 backend 装配 (P4 落地)

`tmuxbot/__main__.py` 按 `TOKEN_TO_BACKEND` 映射, 按 binding 的 `bot_token_env` 分组,
每组创建一个 `TelegramFrontend` 实例并发 polling:

```python
TOKEN_TO_BACKEND = {
    "TG_BOT_TOKEN":       "claude_code",   # @ztl_claude_bot
    "TG_CODEX_BOT_TOKEN": "codex",          # @ztlgpt_bot
}
```

- 一个 bot ↔ 一个 backend ↔ N 个 tmux 子线程 (Boss 架构原则)
- 启动时验证 binding.backend 跟其 bot_token_env 推断一致, 不一致强制对齐 + WARNING
- 多 frontend 并发 polling, 互不干扰

---

## 3. 当前包结构 (P3 重构后, 2026-05-27)

```
tmuxbot/                       ← root project
├── tmuxbot.py                 ← thin entry (16 行) 调 package
├── tmuxbot.py.p2.5.bak        ← 旧单文件备份 (1911 行) — 出问题时回退用
├── tmuxbot/                   ← 新 package
│   ├── __init__.py            ← version
│   ├── __main__.py            ← 装配入口 (136 行): backends + frontend → fire tailer + heartbeat
│   ├── config.py              ← .env + bindings.yaml + offsets.json → State (37 行)
│   ├── state.py               ← Binding + State + fire() (85 行)
│   ├── utils.py               ← encode_cwd / strip_decorations / cwidth / cpad / render_table / utf16_len / load/save_offsets debounced (118 行)
│   ├── tmux.py                ← tmux_send_text (async) / send_key / capture / has_session / pane_command (64 行)
│   ├── picker.py              ← PICKER_BOTTOMBAR_RE / extract_picker_block / detect_idle_picker (88 行)
│   ├── jsonl.py               ← jsonl_poll_loop + on_tmux_event (★ 含 tool_aggregator) (172 行)
│   ├── heartbeat.py           ← heartbeat_typing_loop (TUI 指纹判活跃) (64 行)
│   ├── commands.py            ← inject_slash_and_capture + capture_and_push (150 行)
│   ├── backends/
│   │   ├── __init__.py
│   │   ├── base.py            ← Backend ABC + CmdOpts (77 行)
│   │   └── claude_code.py     ← ClaudeCodeBackend: parse_event / parse_* / find_active_jsonl / ensure_running / find_tui_activity_fp / aggregate_usage (500 行)
│   └── frontends/
│       ├── __init__.py
│       ├── base.py            ← Frontend ABC (47 行)
│       └── telegram.py        ← TelegramFrontend: aiogram Bot + Dispatcher + ack middleware + 所有 handlers + send_html/edit_html/send_pre/send_chat_action/send_picker_card (671 行)
```

**总计 ~2217 行** (vs 单文件 1911 行)。多出来约 300 行是抽象层 boilerplate (Backend/Frontend ABC + 入参传递),换来:
- backend 切换只改 binding.yaml 的 `backend: codex` 一行
- frontend 切换只改 `__main__.py` 装配处一行
- 单文件 testability 改善 (每模块可单测)

> 路径:**所有 `import` 走 `tmuxbot.xxx`**, 例如 `from tmuxbot.state import S`。
> 旧调用方式 `from tmuxbot.py 顶层 import xxx` 已废, 顶层 `tmuxbot.py` 只是 thin entry。

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
