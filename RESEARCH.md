# tmuxbot — 调研归档(精简版)

> 只记 **WHY** + 关键踩坑 + 引用。**HOW** 在 `DEVELOPMENT.md`。
> 整理日期:2026-05-26。

## 1. 路线选择:tmux 注入派,不是 subprocess 派

经全网调研发现 Claude Code 桥接有**两条根本不同路线**:

| 路线 | 代表 | 我们? |
|---|---|---|
| **tmux 注入派**(长跑 TUI + send-keys + JSONL polling) | ccbot / ccgram / 本项目 | ✅ |
| **subprocess 派**(`claude -p --output-format stream-json -c` 每条消息一新子进程) | OpenClaw / TinyClaw / ClaudeClaw / terranc | ❌ |

**为什么选 tmux 注入**:Boss 要"远程操控本机已运行的 claude TUI",`tmux a -t` 能看原貌,90+ slash 命令(`/compact /context /agents`)全部原生生效。subprocess 派代码更少(~200 行)但**没有 TUI**,picker 类命令(`/model`/`/agents`)失效。

## 2. 替代方案否决

| 方案 | 为什么不行 |
|---|---|
| 官方 Claude Code Channels | ① 不能注入已运行会话 ② Esc/Ctrl+C/slash 失效 ③ 终端看不到 bot 回复(破坏 source-of-truth)④ 消息丢失重灾区(Issue #36477/#47153 OPEN) |
| CCGram | Boss 实测不支持 DM(强依赖 Forum Topics) |
| CCBot | `bot.py:580-585` 明确拒绝 DM |
| NoneBot / AstrBot / errbot | 都是"event-driven LLM API 调用"型,跟"tmux 长跑 CLI 子进程"方向错位,套上去和框架打架 |
| Anthropic SDK 路线(terranc) | 消耗 bot 端 token、失去 TUI + MCP + settings、不能注入已有会话 |

## 3. 关键踩坑(实测/Issue 验证)

### Claude Code

- **`/` 和 `.` 在 cwd 编码里都变 `-`**(`/home/pyadmin/.openclaw` → `-home-pyadmin--openclaw`)
- **jsonl 文件名 = session_id**;每事件自带 `sessionId` + `cwd`,**不用反查**
- **9 种 event type**(本机实测):user/assistant/attachment/last-prompt/permission-mode/ai-title/system/file-history-snapshot/queue-operation
- **`/clear` 创建新 jsonl + 新 session_id**(不是 UI 重置),需 fs watcher 监听新文件 — [Issue #37451](https://github.com/anthropics/claude-code/issues/37451)
- **`/btw` `/recap` ephemeral,不入 JSONL** — 必须 capture-pane 兜底
- **同 cwd 多 claude 进程会撞 jsonl** — 强制 cwd 全局唯一

### `--dangerously-skip-permissions`

- **`--resume` 不保留 flag** — 每次启动 / resume 都显式重传 — [Issue #21974](https://github.com/anthropics/claude-code/issues/21974)
- **root/sudo 拒启动** — [Issue #9184](https://github.com/anthropics/claude-code/issues/9184)
- **subagent 不继承 bypass** — Task 工具派的子 agent 仍弹审批 — [Issue #40241](https://github.com/anthropics/claude-code/issues/40241)
- **PreToolUse hook 会重置 bypass** mid-session — [Issue #37745](https://github.com/anthropics/claude-code/issues/37745)
- **启动 WARNING 弹窗** — `~/.claude/settings.json` 设 `skipDangerousModePermissionPrompt: true`
- **`rm -rf /` `rm -rf ~`** 仍被内置拦截(电路熔断)

### tmux

- **`paste-buffer -p` + sleep 0.5s + Enter** 必须分两步,否则 TUI 把 Enter 当 newline — ccbot `tmux_manager.py:241-298`
- **`extended-keys-format csi-u` 会丢 paste 换行** — `set -s extended-keys-format xterm` — [Issue #43169](https://github.com/anthropics/claude-code/issues/43169)
- **>2KB send-keys 灌文本会卡 UI** — 改写临时文件 + `@path` 让 claude 读
- **`pipe-pane` 抓 TUI = 字节流地狱**(alternate screen + 重绘)— 用 JSONL 不用 pipe-pane

### Telegram + aiogram v3

- **Forum General 频道 `message_thread_id = None`** — 回发不能传 thread_id
- **Privacy Mode 默认开** — bot 设为群管理员自动绕过(不用 BotFather)
- **4096 是 UTF-16 单位**(BMP 内中文=1,emoji=2)
- **aiogram v3 无内置 throttler** — 自写 RetryMiddleware 挂 `bot.session.middleware`
- **HTML 完胜 MarkdownV2** — 只转 `<>&`;MarkdownV2 要转 18 字符
- **同一 chat 1 msg/s** / 同一 group 20 msg/min / 不同 chat 30 msg/s
- **超 8KB 转 `send_document`** — `BufferedInputFile` 发 .txt 附件

## 4. 真实痛点排行(全网调研用户反馈,按频次)

1. **消息丢失 / dispatch 失败**(官方 Channels 重灾区)→ 我们用 JSONL 轮询天然无此问题
2. **审批断点**(被迫开 dangerously)→ 接受这个代价,ACL 双重校验兜底
3. **进度不可见**(长任务期间 TG 端干等)→ JSONL 每条 assistant block 立即推
4. **长输出截断**(MCP/SDK 路线)→ 我们 jsonl 解析无截断
5. **polling 死锁**(httpx pool timeout)→ 单实例文件锁 + 退避

## 5. 关键决策汇总

| 决策点 | 选择 | 一句话理由 |
|---|---|---|
| 是否自研 | 自研 | 官方/CCGram/CCBot 都不支持 DM |
| 路线 | tmux 注入派 | 要 TUI 原貌 + 全 slash 命令 |
| 项目形态 | 单文件 `tmuxbot.py` ~500-700 行 | Boss 明确"代码越简单越好" |
| 架构层数 | 1 层(纯函数 + 几个 class) | 砍 Adapter/Card/Palette/EventBus |
| Bot 库 | aiogram v3,长轮询 | 现代 async,不要公网 |
| Markdown | HTML | 转义 3 字符 vs MarkdownV2 18 字符 |
| JSONL 监听 | polling 0.5s | 跨平台稳,ccbot 验证 |
| 注入 | `paste-buffer -p` + 0.5s + Enter | 必须的时序 |
| 捕获 | JSONL 主 + `/screen` capture 兜底 | 结构化无 ANSI 噪声 |
| 启动 flag | `--dangerously-skip-permissions`,resume 重传 | Boss 要免审批 |
| 启动 user | 普通用户,绝不 root | Issue #9184 |
| 项目 hooks | 禁配 PreToolUse | Issue #37745 |
| cwd 唯一性 | 强制 | 防 jsonl 撞 |
| 命令面板 | Telegram 原生 `set_my_commands` | 不做卡片 / palette |
| 危险命令 | bot 端不挡(Boss 在本端打 `/clear` 想清就清) | 远程也一样,信 Boss |
| 二期 Codex / 飞书 | 不留接口 | Rule of Three,YAGNI |

## 6. 借鉴清单

**抄 CCBot**(`github.com/six-ddc/ccbot`):
- 0.5s 注入时序、首次 seek-to-end、offset 损坏防御、4096→3000 切片、代码块自闭合

**抄 OpenClaw / TinyClaw**(`github.com/openclaw/openclaw` / `github.com/jlia0/tinyclaw`):
- 10 行 dispatch 风格的 stream-json 事件解析(JSONL 字段同构,模板直接用)
- 单一 settings 文件原则(不学 ccbot 三份持久化文件)
- DM 默认共享上下文设计

**不抄**:
- ccgram 强依赖 Forum Topics(我们要支持 DM)
- OpenClaw 的 SDK/subprocess 路线、SQLite queue、SSE bus、pairing code、skills/cron、dashboard
- NoneBot/AstrBot 的 plugin/event-bus 框架开销

## 7. 不在 MVP 范围(明确)

- 二期 / 三期:Codex Adapter、飞书 Adapter、Discord、ask_user MCP 审批按钮、Voice/Whisper、Web 面板、跨机部署、录屏、SessionStart hook 强制安装
- **真要做时按 Rule of Three 重新设计抽象**,不预留接口

## 8. 引用

- [CCBot](https://github.com/six-ddc/ccbot) / [CCGram](https://github.com/alexei-led/ccgram) / [OpenClaw](https://github.com/openclaw/openclaw) / [TinyClaw](https://github.com/jlia0/tinyclaw) / [cc-connect](https://github.com/chenhg5/cc-connect) / [heyagent](https://github.com/gergomiklos/heyagent)
- [Claude Code CLI reference](https://code.claude.com/docs/en/cli-reference) / [Sessions](https://code.claude.com/docs/en/sessions) / [Permission modes](https://code.claude.com/docs/en/permission-modes) / [Commands](https://code.claude.com/docs/en/commands)
- [aiogram v3 docs](https://docs.aiogram.dev/en/dev-3.x/) / [Telegram Bot API](https://core.telegram.org/bots/api) / [Forums](https://core.telegram.org/api/forum)
- 关键 Issue:[#21974](https://github.com/anthropics/claude-code/issues/21974) / [#37451](https://github.com/anthropics/claude-code/issues/37451) / [#37745](https://github.com/anthropics/claude-code/issues/37745) / [#9184](https://github.com/anthropics/claude-code/issues/9184) / [#40241](https://github.com/anthropics/claude-code/issues/40241) / [#43169](https://github.com/anthropics/claude-code/issues/43169) / [#36477](https://github.com/anthropics/claude-code/issues/36477)
- JSONL 格式分析:[Inside Claude Code - databunny](https://databunny.medium.com/inside-claude-code-the-session-file-format-and-how-to-inspect-it-b9998e66d56b)

## 9. 决策时间线

- **2026-05-25** 起势 → Anthropic Channels 调研 → 否决 → 多通道路由 + 绑定模型设计
- **2026-05-26** 第一波深挖(JSONL 实测 / ccbot 源码 / Forum Topics / claude resume / TG 限制)→ 写 1742 行文档
- **2026-05-26** 第二波(全 slash 命令 / dangerously flag 兼容性)→ 文档膨胀到顶
- **2026-05-26** Boss 提出"可插拔 + 卡片菜单"→ 第三波(Bot 框架对比 / 项目反馈 / Card 抽象 / 过度设计 / Codex 同构 / OpenClaw)→ **判定:过度设计了**
- **2026-05-26** Boss 一句话拉回极简 → 路线确认 A.tmux 注入派 + 单文件 + 砍所有抽象 → 文档瘦身到现状
