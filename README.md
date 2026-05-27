# tmuxbot

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/)

> Telegram ↔ tmux 内 AI CLI 双向桥, 可插拔架构。
> 在 TG 任意端点 (DM / 群组 / 论坛话题) 发消息 → 注入 tmux pane 里的 AI cli (Claude Code / Codex) → cli 输出实时回推同一端点。
> **不调 API, 不消费 token** — bot 只搬键盘 + 屏幕, 所有 token 在 cli 那一头花。

---

## 这是什么?

把 **本地跑的 AI 编程 CLI** (Claude Code / Codex) **远程化**:

- 不在电脑前时, 用手机 TG 推动 AI 跑代码、读文件、改项目
- 本地 tmux session 跑 AI cli 不变, 离开电脑后 cli 也不会断
- 多项目并行: 同一个 bot 接 N 个 tmux 子线程 (每个项目一个 cwd)
- 多 cli 共存: claude 用一个 bot, codex 用另一个 bot, 各管各的(一个 bot ↔ 一种 cli)

跟"调 API 的 chat bot"不同 — tmuxbot 不出 token 钱, 完全复用 cli 自己的订阅 / 配额。

---

## 入口文档

| 文档 | 用途 |
|---|---|
| **[CLAUDE.md](./CLAUDE.md)** | 项目宪法 (协作铁律 + 关键事实 + 已知陷阱 + §9 决策日志) |
| [DEVELOPMENT.md](./DEVELOPMENT.md) | 开发文档 (模块结构 + 命令清单 + 部署红线 + Milestone) |
| [CODE_REVIEW.md](./CODE_REVIEW.md) | 地毯级代码审查 (25 类问题清单 + 修复路径) |
| [RESEARCH.md](./RESEARCH.md) | 立项调研 (技术选型推理) |

---

## 30 秒上手

```bash
# 1. 装依赖
pip install -e .

# 2. 配凭证
cp .env.example .env
vim .env                    # 填 TG_BOT_TOKEN (可选 TG_CODEX_BOT_TOKEN) 和 BOSS_USER_ID
vim bindings.yaml           # 填 chat_id / tmux_session / cwd / bot_token_env / backend

# 3. 跑起来
bash bin/restart.sh         # tmux session 包一层 + 第一次失败自动重试 + 心跳验证

# 4. 看状态
bash bin/status.sh          # 进程 + session + 日志末尾 + 心跳

# 停止
bash bin/stop.sh
```

在 TG 发 `/whoami`, bot 回 user_id / chat_id / thread_id → 通了。

### 生产部署 (systemd, 推荐)

```bash
# 一次性安装 systemd user service
mkdir -p ~/.config/systemd/user
ln -sf "$(pwd)/deploy/systemd/tmuxbot.service" ~/.config/systemd/user/tmuxbot.service
systemctl --user daemon-reload
systemctl --user enable --now tmuxbot.service
loginctl enable-linger $USER   # logout 后继续跑

# 看 log / 重启 / 停
journalctl --user -u tmuxbot -f
systemctl --user restart tmuxbot
systemctl --user stop tmuxbot
```

bot crash 后 5 秒内自动拉起,无需手动守护。

---

## 当前能力

- **多前端 / 多后端可插拔架构**
  - backends: claude_code (Claude Code), codex (OpenAI Codex CLI)
  - frontends: telegram (M4 计划加飞书)
- **双 bot 共存**: `1 bot ↔ 1 cli ↔ N tmux 子线程` — 通过 bot_token_env 路由
- **TG 全场景**: DM / 普通群 / supergroup forum topic, 通过 `(chat_id, thread_id)` 唯一映射 binding
- **核心命令**: `/status` `/info` `/whoami` `/new` `/resume` `/rename` `/esc` `/cc` `/eof` `/screen` `/restart`
- **TUI 透传命令**: `/context` `/cost` `/usage` `/compact` `/clear` 等带专属 parser, 抓屏结构化反馈
- **工具调用聚合**: 一个 turn 内的 tool_use 流式刷同一条 TG 消息, 真说话单独 push 触发通知
- **picker 兜底**: claude TUI 事务式 flush jsonl 导致 picker 不可见时, 屏幕 OCR 抓 picker 字符画推 inline keyboard
- **活性指示**: TUI 状态行「时间 + token」指纹判活跃, 干活中显示 typing, idle 不显示, bot 死 5s 内 typing 消失
- **消息已读反应**: 收到 Boss 消息立刻加 👀 emoji (Bot API 7.0+)
- **健壮性**: jsonl tailer + GC 强引用修复 + offsets debounce + tmux 注入 async 不阻塞 event loop

---

## 架构图

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
                              tools/text 事件)                    + send-keys Enter
                              │
                              ▼
                       工具调用聚合器 (edit 同一条 TG 消息)
                       真说话 (单独发触发 push 通知)
```

---

## 兼容性

- Python 3.10+ (用 `match` 语法和新类型注解)
- 依赖: `aiogram>=3.13` / `pyyaml>=6.0` / `python-dotenv>=1.0`
- Telegram Bot API 7.0+ (`setMessageReaction` 需要)
- 已测试 cli: Claude Code, Codex CLI (`@openai/codex`) 0.124+

---

## 路线图

- **M1** ✅ 单文件骨架 + 双 binding + 命令组 + heartbeat
- **M2** ✅ 代码审查 + 可插拔重构 (`core/` + `backends/` + `frontends/`)
- **M3** ✅ 接入 Codex CLI (CodexBackend) + 双 bot 共存 (1 bot ↔ 1 backend ↔ N tmux 子线程)
- **M4** ⏳ 接入飞书前端 (FeishuFrontend, 沿用 frontends/base.py 接口)

---

## License

[MIT](./LICENSE)
