# tmuxbot

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/)

> Telegram + 飞书 ↔ tmux 内 AI CLI(Claude Code / Codex)双向桥 —— 远程在 IM 发消息推动本地 tmux pane 里的 cli,cli 输出实时回推同端点。
>
> **不调 API、不走 headless `claude -p` / SDK 路径、用 tmux pane TUI 注入** —— 保留本地交互式 CLI 作为唯一执行面。

---

## 为什么需要 tmuxbot?(2026-06-15 背景)

Anthropic 文档说明:从 **2026-06-15** 起,Claude 订阅用户的 **Agent SDK / `claude -p` / Claude Code GitHub Actions / 第三方 Agent SDK app** 会走独立的 Agent SDK monthly credit;交互式 Claude Code terminal / IDE 继续走原订阅 usage limits。

| 明确走 Agent SDK credit | 文档说明仍走交互式订阅 usage limits |
|---|---|
| Claude **Agent SDK** | 交互式 Claude Code terminal / IDE |
| `claude -p` headless / `--print` mode | 在 IDE 插件里用 Claude Code |
| Claude Code **GitHub Actions** | Claude web / desktop / mobile conversations |
| 基于 **Agent SDK 的第三方应用** | — |

很多 IM ↔ Claude bridge 采用 Agent SDK 或 `claude -p` headless 子进程路线,这类路径已经被官方明确归入 Agent SDK credit。tmuxbot 的设计目标是避开这些 headless/programmatic 执行面,只远程控制本机已经存在的交互式 TUI。

**tmuxbot 用 tmux pane TUI 注入:**

- bot 通过 `tmux paste-buffer` 把消息粘到 pane 里
- pane 里的 `claude` / `codex` 是**正常 TUI 模式跑**,不是 `-p` / SDK
- jsonl 写到 `~/.claude/projects/<encoded-cwd>/*.jsonl`,跟人手动跑完全一样

这不是官方政策承诺,而是项目的工程边界:不调用 vendor API、不派 headless 子进程、不把 IM bridge 做成 Agent SDK app。是否以及如何计量最终以各 CLI/vendor 的实际规则为准。

这是 tmuxbot 区别于 SDK/headless bridge 的核心价值。

---

## 这是什么?

一个 Python(3.10+)的 IM ↔ AI CLI 双向桥,可插拔架构:

- **前端(IM)**:Telegram、飞书(lark-oapi WebSocket 长连接)
- **后端(AI CLI)**:Claude Code,OpenAI Codex CLI
- **架构原则**:1 bot ↔ 1 backend ↔ N 个 tmux 子线程(同类 CLI 多项目并行)

### 真正实用的场景

- 不在电脑前时,用手机 TG 或飞书推动本地 AI 跑代码 / 改项目 / 看日志
- 多项目并行:每个项目一个 tmux session 一个 cwd,各自加载项目自己的 `CLAUDE.md`
- 多 cli 共存:claude 用一个 bot,codex 用另一个 bot,各管各的

---

## 30 秒上手

```bash
# 1. 装依赖
pip install -e .
# 接飞书额外装:
pip install -e ".[feishu]"

# 2. 配凭证
cp .env.example .env
vim .env                    # 填 TG_BOT_TOKEN,BOSS_USER_ID;接 codex 再加 TG_CODEX_BOT_TOKEN;接飞书加 FEISHU_APP_ID/SECRET/BOSS_OPEN_IDS

# 3. 配 binding(IM 端点 ↔ tmux pane 映射)
cp bindings.example.yaml bindings.yaml
vim bindings.yaml           # 改 chat_id / tmux_session / cwd / backend / bot_token_env / channel(模板含 TG/codex/飞书 5 个示例)

# 4. 启动
bash bin/restart.sh         # 含失败自动重试 + 心跳验证

# 5. 看状态
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

- **双前端**:Telegram(DM / 普通群 / supergroup forum topic)+ 飞书(群聊 / 私聊,interactive card 收发/编辑)
- **双 bot 共存**:`@your_claude_bot` 接 claude_code,`@your_codex_bot` 接 codex
- **核心命令**:`/status` `/info` `/whoami` `/new` `/resume` `/rename` `/esc` `/cc` `/eof` `/screen` `/restart`
- **TUI 透传**:`/context` `/cost` `/usage` `/compact` `/clear` 等,抓屏结构化反馈
- **工具调用聚合**:一个 turn 内的 tool_use 流式刷同一条 IM 消息,真说话单独 push 触发通知
- **picker 兜底**:claude TUI 事务式 flush jsonl 导致 picker 不可见时,屏幕 OCR 抓 picker 字符画推 inline keyboard
- **活性指示**:TUI 状态行「时间 + token」指纹判活跃,工作中显示 typing(Telegram);飞书无 typing API
- **消息已读反应**:TG 👀 emoji(Bot API 7.0+);飞书 👀 OnIt reaction
- **订阅配额**:`/status` 展示 5h/7d 五窗口 utilization + 精确重置倒计时(走 OAuth API)
- **健壮性**:tmux paste 等 TUI idle 才 send Enter;jsonl tailer 积压保护(512KB 阈值);GC 强引用修复;offsets debounce 写盘

---

## 架构

```
TG 用户                飞书用户
  │                       │
  ├─ @claude_bot ─┐   ┌─ 飞书 App ─┐
  │               │   │            │
  ▼               ▼   ▼            ▼
TelegramFrontend      FeishuFrontend
(aiogram polling)     (lark-oapi WebSocket)
  │                       │
  └───────────────────────┘
              │
        dispatch.py (共享命令分发层)
              │
     ┌────────┴────────┐
     │                 │
ClaudeCodeBackend  CodexBackend
     │                 │
     └────────┬────────┘
              │
         tmux pane(s)
     paste-buffer inject
     → TUI idle 轮询 → Enter
              │
         jsonl tailer
     parse_event + aggregator
              │
        推回 IM 前端
```

技术细节看 [DEVELOPMENT.md](./DEVELOPMENT.md)。

## 维护质量

```bash
make install-dev
make check
```

长期产品化路线看 [PRODUCTIZATION.md](./PRODUCTIZATION.md)。

---

## 路线图

- **M1** ✅ 单文件骨架 + 双 binding + 命令组 + heartbeat
- **M2** ✅ 代码审查 + 可插拔重构(`backends/` + `frontends/` + `dispatch.py`)
- **M3** ✅ 接入 Codex CLI + 双 bot 共存(1 bot ↔ 1 backend ↔ N tmux 子线程)+ systemd 部署
- **M4** ✅ 接入飞书前端(lark-oapi WebSocket + interactive card)+ 多实例支持

---

## License

[MIT](./LICENSE)
