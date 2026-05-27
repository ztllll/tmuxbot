# tmuxbot

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/)

> Telegram ↔ tmux 内 AI CLI(Claude Code / Codex)双向桥 —— 远程在 TG 发消息推动本地 tmux pane 里的 cli,cli 输出实时回推同端点。
>
> **不调 API、不消费 token、用 tmux pane TUI 注入(模拟人手敲键盘)** —— 绕开 6 月 15 日起的 programmatic 限制。

---

## 为什么需要 tmuxbot?(2026-06-15 价值主张)

**Anthropic 2026-06-15 新政:** Claude 付费 plan 把 **programmatic 使用** 单独划成一个独立 credit:

| 走 programmatic credit(将受额度限制) | 走普通订阅(不受影响) |
|---|---|
| Claude **Agent SDK** | 在终端跑 `claude` TUI(人坐在电脑前) |
| `claude -p` headless / `--print` mode | 在 IDE 插件里用 Claude Code |
| Claude Code **GitHub Actions** | 用 `/resume <id>` 在 TUI 里恢复对话 |
| 基于 **Agent SDK 的第三方应用** | — |
| 任何 IM bridge **走 `claude -p` 子进程** | — |

**已有的 IM ↔ Claude bridge 项目(基于 Agent SDK / `claude -p`)6.15 后会撞 credit 天花板。** 比如 `claude-code-im-channel` 这类项目,飞书 / Discord / Telegram 都是用 `claude -p` headless 跑的 — 6.15 后用着用着会限速 / 要补差价。

**tmuxbot 用 tmux pane TUI 注入,跟人手动敲键盘等价:**

- bot 通过 `tmux paste-buffer` 把消息粘到 pane 里
- pane 里的 `claude` / `codex` 是**正常 TUI 模式跑**,不是 `-p` / SDK
- jsonl 写到 `~/.claude/projects/<encoded-cwd>/*.jsonl`,跟人手动跑完全一样
- 在 Anthropic 视角看,就是"一个人在终端用 claude",**不算 programmatic**

→ **6.15 后继续走普通订阅,无 credit 限制。**

这是 tmuxbot 区别于其他 IM bridge 的核心价值,也是它存在的唯一理由。

---

## 这是什么?

一个 Python(3.10+)的 IM ↔ AI CLI 双向桥,可插拔架构:

- **前端(IM)**:Telegram(已实现),飞书(规划中)
- **后端(AI CLI)**:Claude Code,OpenAI Codex CLI
- **架构原则**:1 bot ↔ 1 backend ↔ N 个 tmux 子线程(同类 CLI 多项目并行)

### 真正实用的场景

- 不在电脑前时,用手机 TG 推动本地 AI 跑代码 / 改项目 / 看日志
- 多项目并行:每个项目一个 tmux session 一个 cwd,各自加载项目自己的 `CLAUDE.md`
- 多 cli 共存:claude 用一个 bot,codex 用另一个 bot,各管各的

---

## 30 秒上手

```bash
# 1. 装依赖
pip install -e .

# 2. 配凭证
cp .env.example .env
vim .env                    # 填 TG_BOT_TOKEN,BOSS_USER_ID;接 codex 再加 TG_CODEX_BOT_TOKEN
vim bindings.yaml           # 填 chat_id / tmux_session / cwd / bot_token_env / backend

# 3. 启动
bash bin/restart.sh         # 含失败自动重试 + 心跳验证

# 4. 看状态
bash bin/status.sh

# 停止
bash bin/stop.sh
```

在 TG 发 `/whoami`,bot 回 user_id / chat_id / thread_id → 通了。

### 生产部署(systemd,推荐)

```bash
mkdir -p ~/.config/systemd/user
ln -sf "$(pwd)/deploy/systemd/tmuxbot.service" ~/.config/systemd/user/tmuxbot.service
systemctl --user daemon-reload
systemctl --user enable --now tmuxbot.service
loginctl enable-linger $USER

# 看日志 / 重启 / 停
journalctl --user -u tmuxbot -f
systemctl --user restart tmuxbot
systemctl --user stop tmuxbot
```

bot crash 后 5 秒内自动拉起,无需手动守护。

---

## 当前能力

- **TG 全场景**:DM / 普通群 / supergroup forum topic,通过 `(chat_id, thread_id)` 唯一映射 binding
- **双 bot 共存**:`@your_claude_bot` 接 claude_code,`@your_codex_bot` 接 codex
- **核心命令**:`/status` `/info` `/whoami` `/new` `/resume` `/rename` `/esc` `/cc` `/eof` `/screen` `/restart`
- **TUI 透传**:`/context` `/cost` `/usage` `/compact` `/clear` 等,抓屏结构化反馈
- **工具调用聚合**:一个 turn 内的 tool_use 流式刷同一条 TG 消息,真说话单独 push 触发通知
- **picker 兜底**:claude TUI 事务式 flush jsonl 导致 picker 不可见时,屏幕 OCR 抓 picker 字符画推 inline keyboard
- **活性指示**:TUI 状态行「时间 + token」指纹判活跃,工作中显示 typing,idle 不显示,bot 死 5 秒内 typing 消失
- **消息已读反应**:收到消息立刻加 👀 emoji(Bot API 7.0+)
- **健壮性**:tmux paste 等 TUI idle 才 send Enter(避开 busy 态 race);jsonl tailer + GC 强引用修复 + offsets debounce 写盘

---

## 架构

```
TG 用户
  │
  ├── @claude_bot ──► TelegramFrontend(backend=claude_code) ──┬─► binding1: tmux/proj-A
  │                                                            └─► binding2: tmux/proj-B
  │
  └── @codex_bot ──► TelegramFrontend(backend=codex)         ──── binding3: tmux/proj-C

       ↑                       ↑                                       ↓
       │                       │                                       │
   消息 ack 反应             jsonl tailer                         tmux paste-buffer
   typing 心跳               (parse_event 区分                    + bracketed paste
   (TUI 指纹判活跃)            tools/text 事件)                     + 等 TUI idle + Enter
                              │                                       (不再被 busy race 卡)
                              ▼
                       工具调用聚合器 (edit 同一条 TG 消息)
                       真说话 (单独发触发 push 通知)
```

技术细节看 [DEVELOPMENT.md](./DEVELOPMENT.md)。

---

## 路线图

- **M1** ✅ 单文件骨架 + 双 binding + 命令组 + heartbeat
- **M2** ✅ 代码审查 + 可插拔重构(`backends/` + `frontends/`)
- **M3** ✅ 接入 Codex CLI + 双 bot 共存(1 bot ↔ 1 backend ↔ N tmux 子线程)
- **M4** ⏳ 接入飞书前端(替代 6.15 后受限的 `bot-im-channel` 等头部 IM bridge)

---

## License

[MIT](./LICENSE)
