# tmuxbot

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/)
[![Version](https://img.shields.io/badge/version-0.3.0-blue.svg)](./VERSIONING.md)

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
uv tool install 'tmuxbot[full]'
tmuxbot serve --open
```

首次运行会自动打开中文 WebUI，并生成 10 分钟有效、设置成功后立即失效的一次性本机授权。没有 `.env`、通道或 binding 时 WebUI 也会保持可用；bridge 显示“尚未配置”。运行 `tmuxbot doctor` 可检查 tmux、Claude Code、Codex 和运行目录。

源码开发、旧 `.env` / `bindings.yaml` 配置和 IM `/whoami` 验证方式仍保留，见 [DEVELOPMENT.md](./DEVELOPMENT.md)。

### 生产部署(systemd,推荐)

推荐先用 Claude Code native installer 安装 `claude`,并在 `.env` 里写绝对路径:

```bash
curl -fsSL https://claude.ai/install.sh | bash
echo "CLAUDE_BIN=$HOME/.local/bin/claude" >> .env
```

`CLAUDE_BIN` 会在拉起 Claude 时读取,避免 systemd/tmux 的非交互 shell `PATH` 找不到 `claude`,也避免命中坏掉的 npm 全局安装。`CODEX_BIN` 同理可指向 codex 绝对路径。

Runtime V2 仍然直接操作 tmux 内的交互式 CLI。建议先配置
`TMUXBOT_RUNTIME_V2=shadow`:线上继续发送兼容路径结果,同时只比较脱敏后的事件结构;
日志无 mismatch 后再切 `on`。`TMUXBOT_CLAUDE_HOOKS=true` 会幂等安装 tmuxbot
自有 Claude hooks,用于会话身份与 Stop 最终回复;hooks 只写本地 spool,不会直接发 IM,
JSONL 和终端状态探测仍继续工作。

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

### Web control plane

推荐统一入口会启动 Web，并按配置状态监督独立 bridge child：

```bash
tmuxbot serve --open
```

默认监听 `127.0.0.1:8765`。`tmuxbot web` 仍可只启动 Web；`tmuxbot bridge` 仍保留严格配置检查。配置、数据和状态默认使用 XDG 目录：`~/.config/tmuxbot`、`~/.local/share/tmuxbot`、`~/.local/state/tmuxbot`。

需要常驻时：

```bash
tmuxbot install-service --now
journalctl --user -u tmuxbot -f
```

**不要把 Web 端口直接暴露到公网。** 远程访问应通过带 TLS 和访问控制的反向代理,
并设置 `TMUXBOT_WEB_SECURE_COOKIE=true` 与准确的 `TMUXBOT_WEB_PUBLIC_ORIGIN`。
`deploy/systemd/tmuxbot-web.service` 提供独立 unit 示例;其中凭证只从 `.env` 读取,
不会出现在 `ExecStart` 命令行。

---

## 当前能力

- **零配置中文 WebUI**:`uv tool install 'tmuxbot[full]'` 后运行 `tmuxbot serve --open`；可扫描/探测 tmux、Claude Code、Codex，登记项目并启动受管 CLI，会话和数据使用 XDG 私有目录
- **原生 Web TUI**:xterm.js 直接 attach 已登记 tmux target，默认只观察；显式接管后才允许键盘输入，断开浏览器不会终止 tmux 会话
- **Web 通道向导**:可为受管会话配置 Telegram 或飞书，密钥只写入本机 `0600` 配置，不通过 API 回显
- **TeamRun 多 LLM**:确定性 Coordinator / Implementer / Reviewer 三角色协作，唯一写租约、DAG、mailbox、Artifact、重试、独立验收和恢复；Implementer 交付证据后 Reviewer 自动收到只读审查包
- **双前端**:Telegram(DM / 普通群 / supergroup forum topic)+ 飞书(群聊 / 私聊,Card JSON 2.0 收发/编辑；操作统一使用 `/` 命令)
- **中文控制面板**:`/panel` 或 `/settings` 主动打开轻量面板，可切换群聊 @ 策略、执行 `/status` `/screen` `/new` `/compact` `/resume` `/esc` `/cc`，并通过当前 tmux CLI 的原生 `/model` 选择器切换模型；面板也提供带二次确认的“重启 CLI”，Codex/Claude 都会恢复原 provider 会话与 transcript，保留上下文；Claude 模型卡额外提供“仅本会话”，避免修改未来新会话默认模型
- **@ 策略命令**:`/mention on` 表示无需 @，`/mention off` 表示必须 @，`/mention default` 恢复部署默认，`/mention status` 查看当前策略；设置按 binding 持久化且立即生效
- **双 bot 共存**:`@your_claude_bot` 接 claude_code,`@your_codex_bot` 接 codex
- **核心命令**:`/status` `/info` `/whoami` `/new` `/resume` `/rename` `/esc` `/cc` `/eof` `/screen` `/restart`
- **TUI 透传**:`/context` `/cost` `/usage` `/compact` `/clear` 等,抓屏结构化反馈
- **工具调用聚合**:一个 turn 内的 tool_use 流式刷同一条 IM 消息,真说话单独 push 触发通知
- **Codex 计划跟随**:`update_plan` 会维护一条可编辑的“当前计划”消息,TG/飞书里持续显示最新 `in_progress` / `pending` / `completed` 状态
- **双向附件**:Telegram/飞书收到的图片/文件会下载到本机并以 `@path` 注入 TUI;AI 回复里的绝对/相对路径、Markdown 文件链接和图片链接会转成原生 IM 附件,聊天内容不暴露服务器绝对路径
- **统一富消息**:Codex/Claude 共用 `ReplyDocument`;Telegram 使用 HTML/可展开引用且不附常驻按钮,飞书使用 Card JSON 2.0 header、summary、状态色和可选 CardKit 流式更新
- **长回复自动分页**:Telegram 按 HTML/UTF-16 安全边界拆成多条消息并保持代码块标签完整；飞书按 Card JSON 2.0 请求大小拆成连续卡片，不再把普通长回复截断成预览或强制改发 TXT
- **Telegram 状态标识**:Telegram 没有飞书式原生彩色卡片标题，使用 `🟡 工作中`、`🟠 等待输入`、`✅ 已完成`、`🔴 错误/阻塞`、`🔵 信息`、`⚪ 状态未知` 作为文本等价呈现
- **飞书状态色**:工作中黄色、等待输入橙色、完成/空闲绿色、错误/阻塞红色、普通信息蓝色、未知状态灰色；流式回复从黄色开始并在成功完成后变为绿色
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

### 富消息与附件配置

飞书默认启用 Card JSON 2.0。需要临时回滚旧卡片时设置
`TMUXBOT_FEISHU_CARD_V2=0`。CardKit 流式更新默认关闭；确认应用已订阅
`card.action.trigger` 并拥有 `cardkit:card:write` 权限后，可设置
`TMUXBOT_FEISHU_STREAMING=1` 灰度启用。多飞书应用可以使用
`FEISHU_CARD_V2`、`FEISHU_STREAMING` 等按 `bot_token_env` 前缀覆盖。

回复里的本地文件只有在以下目录内才会自动上传：binding 的 `cwd`、
`TMUXBOT_ATTACHMENT_DIR`、操作系统临时目录，以及
`TMUXBOT_ATTACHMENT_ALLOWED_ROOTS` 明确配置的目录。额外目录在 Linux 上使用
冒号分隔。目录、设备、socket、不存在的文件和安全根之外的路径不会上传；
上传失败只向聊天显示安全文件名，不显示服务器绝对路径。

## 维护质量

```bash
make install-dev
make check
```

持续迭代入口:

- [CHANGELOG.md](./CHANGELOG.md):变更记录
- [VERSIONING.md](./VERSIONING.md):版本号与发布标签策略
- [RELEASE.md](./RELEASE.md):发布检查清单
- [CONTRIBUTING.md](./CONTRIBUTING.md):贡献与 PR 要求
- [SECURITY.md](./SECURITY.md):安全边界与敏感文件规则
- [SUPPORT.md](./SUPPORT.md):issue/support 信息收集指南
- [PRODUCTIZATION.md](./PRODUCTIZATION.md):长期产品化路线

---

## 路线图

- **M1** ✅ 单文件骨架 + 双 binding + 命令组 + heartbeat
- **M2** ✅ 代码审查 + 可插拔重构(`backends/` + `frontends/` + `dispatch.py`)
- **M3** ✅ 接入 Codex CLI + 双 bot 共存(1 bot ↔ 1 backend ↔ N tmux 子线程)+ systemd 部署
- **M4** ✅ 接入飞书前端(lark-oapi WebSocket + interactive card)+ 多实例支持

---

## License

[MIT](./LICENSE)
