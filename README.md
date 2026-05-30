# tmuxbot

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/)

> Telegram + 飞书 ↔ tmux 内 AI CLI(Claude Code / Codex)双向桥 —— 远程在 IM 发消息推动本地 tmux pane 里的 cli,cli 输出实时回推同端点。
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

- **前端(IM)**:Telegram、飞书(lark-oapi WebSocket 长连接)
- **后端(AI CLI)**:Claude Code,OpenAI Codex CLI
- **架构原则**:1 bot ↔ 1 backend ↔ N 个 tmux 子线程(同类 CLI 多项目并行)

### 真正实用的场景

- 不在电脑前时,用手机 TG 或飞书推动本地 AI 跑代码 / 改项目 / 看日志
- 多项目并行:每个项目一个 tmux session 一个 cwd,各自加载项目自己的 `CLAUDE.md`
- 多 cli 共存:claude 用一个 bot,codex 用另一个 bot,各管各的
- 内存受限机器:配 `idle_kill_seconds` 闲置自动杀 claude,来消息 `--resume` 重生,上下文不丢

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
- **idle-kill**:配 `idle_kill_seconds` 闲置自动杀 claude,来消息 `--resume` 重生,节省内存
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

---

## 路线图

- **M1** ✅ 单文件骨架 + 双 binding + 命令组 + heartbeat
- **M2** ✅ 代码审查 + 可插拔重构(`backends/` + `frontends/` + `dispatch.py`)
- **M3** ✅ 接入 Codex CLI + 双 bot 共存(1 bot ↔ 1 backend ↔ N tmux 子线程)+ systemd 部署
- **M4** ✅ 接入飞书前端(lark-oapi WebSocket + interactive card)+ idle-kill + 多实例支持

---

## License

[MIT](./LICENSE)
