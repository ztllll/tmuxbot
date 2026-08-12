# tmuxbot

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/)
[![Version](https://img.shields.io/badge/version-0.3.0-blue.svg)](./VERSIONING.md)

> Telegram + 飞书 ↔ tmux 内 AI CLI(Claude Code / Codex / Pi)双向桥 —— 远程在 IM 话题发消息推动精确 tmux pane 里的 CLI,输出实时回推同一话题。
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
- **后端(AI CLI)**:Claude Code、OpenAI Codex CLI、Pi
- **架构原则**:一个 IM credential 管多个精确 topic route;每个 route 绑定一个 tmux pane 并选择自己的 adapter

### 真正实用的场景

- 不在电脑前时,用手机 TG 或飞书推动本地 AI 跑代码 / 改项目 / 看日志
- 多项目并行:每个项目一个 tmux session 一个 cwd,各自加载项目自己的 `CLAUDE.md`
- 多 CLI 共存:同一个项目群的不同话题可分别接 Claude、Codex、Pi，SSH attach 的仍是同一批真实 pane

---

## 30 秒上手

```bash
uv tool install 'tmuxbot[full]'
tmuxbot serve --open
```

首次运行会自动打开中文 WebUI，并生成 10 分钟有效、设置成功后立即失效的一次性本机授权。没有 `.env`、通道或 binding 时 WebUI 也会保持可用；bridge 显示“尚未配置”。运行 `tmuxbot doctor` 可检查 tmux、Claude Code、Codex、Pi 和运行目录。

源码开发、旧 `.env` / `bindings.yaml` 配置和 IM `/whoami` 验证方式仍保留，见 [DEVELOPMENT.md](./DEVELOPMENT.md)。通过 Boss DM 用自然语言创建或绑定 tmux 与 Telegram/飞书话题，见 [Admin DM 运维指南](./docs/admin-dm-operations.md)；完整 route 模型见 [Topic Routes and Admin DM](./docs/topic-routing.md)。

为避免弱模型手工拼错 YAML、thread ID、tmux 或 systemd，Admin 会话应安装统一的 **Admin Operations Contract**，并只调用确定性事务命令：

```bash
tmuxbot admin --file /path/to/bindings.yaml --service tmuxbot.service \
  install-contract --cwd /home/you
tmuxbot admin --file /path/to/bindings.yaml --service tmuxbot.service \
  provision-project --name demo-pi --channel telegram \
  --credential TG_CODEX_BOT_TOKEN \
  --topic-link https://t.me/c/CHAT/THREAD \
  --cwd /absolute/project/demo --backend pi
# 新话题则改用 --chat-id + --topic-title；核对 plan 后原命令增加 --apply。
# inventory / telegram-topic / feishu-topics / create-topic / bind-topic / verify
# 保留为低层诊断、恢复和迁移接口。
```

事务内部负责完整候选校验、原子 YAML 替换、监督服务重启、post-apply verify 和失败回滚；LLM 只负责提供明确的 endpoint、target、cwd 与 adapter。

### 生产部署(systemd,推荐)

推荐先用 Claude Code native installer 安装 `claude`,并在 `.env` 里写绝对路径:

```bash
curl -fsSL https://claude.ai/install.sh | bash
echo "CLAUDE_BIN=$HOME/.local/bin/claude" >> .env
```

`CLAUDE_BIN` 会在拉起 Claude 时读取,避免 systemd/tmux 的非交互 shell `PATH` 找不到 `claude`,也避免命中坏掉的 npm 全局安装。`CODEX_BIN` 与 `PI_BIN` 同理可指向绝对路径。

Codex 的模型不由 tmuxbot 写死。每次新建或恢复 Codex TUI 时，tmuxbot 都读取
`~/.codex/config.toml` 顶层 `model`，存在时传递 `-m <model>`；因此只要修改 Codex
自身配置，之后的新会话和恢复会话都会自动使用新默认模型。配置缺失或无效时会省略 `-m`，交由 Codex 原生默认行为处理。

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

# 或由安装器生成同一个 unit，并启用 tmux/provider 常驻自愈
# tmuxbot install-service --now --self-heal

# 看日志 / 重启 / 停
journalctl --user -u tmuxbot -f
systemctl --user restart tmuxbot
systemctl --user stop tmuxbot
```

生产主机只应启用一个 `tmuxbot.service`。同一进程可承载多个 Telegram Bot 和飞书
App credential；每个飞书 App 使用隔离的 SDK event loop，避免 `lark-oapi` 的模块级
loop 在多 App 间互相覆盖。旧版 `tmuxbot-bridge-refresh@*.timer` 不再需要：安装器会
停用并删除该周期强制重启 helper，连接恢复由 frontend 自身重连循环负责。

`Restart=always` 会在主进程或 bridge child 异常退出后 5 秒内自动拉起；unit 关闭
systemd start-limit，瞬时连续故障不会让服务永久停在 failed。运行中可在 WebUI 的
`/api/channel-health` 查看只读连接审计（连接时间、最近收包、最近有效入站、恢复次数）。

tmux 会话默认按消息懒启动：手动执行 `tmux kill-session -t <name>`，或在
Telegram/飞书发送 `/tmuxstop` 后，后台不会周期性复活它；下一条消息到达时会自动重建
tmux，并恢复已绑定的 Claude/Codex/Pi provider 会话。需要常驻自愈时，可显式设置
`TMUXBOT_LIFECYCLE_ENABLED=1`，或用 `tmuxbot install-service --now --self-heal`
写入 unit。watchdog 默认每 30 秒核对 route target，缺失时按持久 provider identity
重建 tmux/CLI；它不会执行 `tmux kill-server`。默认每 60 分钟仅巡检**已存在**的 route pane：
验证 provider 前台进程与健康状态，必要时仅恢复该 route 的异常 provider 进程树。它不会按空闲
时间退出 Claude/Codex/Pi，也不会重建被人工关闭的 tmux；缺失 target 仍等下一条精确 route 消息
按需恢复。Telegram、飞书和 Web 控制面板都提供带确认的“关闭 tmux”操作，管理记录和历史不会被删除。旧多服务主机的合并、offset 防回吐、
回滚和验收步骤见 [`docs/single-service-operations.md`](docs/single-service-operations.md)。

### Web control plane

推荐统一入口会启动 Web，并按配置状态监督独立 bridge child：

```bash
tmuxbot serve --open
```

默认监听 `127.0.0.1:8765`。`tmuxbot web` 仍可只启动 Web；`tmuxbot bridge` 仍保留严格配置检查。配置、数据和状态默认使用 XDG 目录：`~/.config/tmuxbot`、`~/.local/share/tmuxbot`、`~/.local/state/tmuxbot`。

需要常驻时：

```bash
tmuxbot install-service --now --self-heal
journalctl --user -u tmuxbot -f
```

**不要把 Web 端口直接暴露到公网。** 远程访问应通过带 TLS 和访问控制的反向代理,
并设置 `TMUXBOT_WEB_SECURE_COOKIE=true` 与准确的 `TMUXBOT_WEB_PUBLIC_ORIGIN`。
`deploy/systemd/tmuxbot-web.service` 提供独立 unit 示例;其中凭证只从 `.env` 读取,
不会出现在 `ExecStart` 命令行。

---

## 当前能力

- **零配置中文 WebUI**:`uv tool install 'tmuxbot[full]'` 后运行 `tmuxbot serve --open`；可验证项目目录、显示 Git 与已有 pane、扫描/探测 tmux、Claude Code、Codex、Pi，登记项目并启动受管 CLI。已有 pane 可先以只读模式直接查看，手动接管后才允许输入；模型候选仍由当前 CLI 的原生 `/model` picker 提供。
- **原生 Web TUI**:xterm.js 直接 attach 已登记 tmux target，默认只观察；显式接管后才允许键盘输入，断开浏览器不会终止 tmux 会话
- **Web 通道向导**:可为受管会话配置 Telegram 或飞书，密钥只写入本机 `0600` 配置，不通过 API 回显
- **TeamRun 多 LLM**:确定性 Coordinator / Implementer / Reviewer 三角色协作，唯一写租约、DAG、mailbox、Artifact、重试、独立验收和恢复；Implementer 交付证据后 Reviewer 自动收到只读审查包
- **双前端**:Telegram(DM / 普通群 / supergroup forum topic)+ 飞书(群聊 / 私聊,Card JSON 2.0 收发/编辑；操作统一使用 `/` 命令)
- **中文控制面板**:`/menu` 主动打开轻量面板（`/panel`、`/settings` 兼容保留），可切换群聊 @ 策略、执行 `/status` `/screen` `/new` `/compact` `/resume` `/esc` `/cc`。模型候选由当前 tmux CLI 的原生 `/model` 选择器实时提供，面板会显示已读取的当前模型。受管 Codex 会话在新建与重启恢复时读取 `~/.codex/config.toml` 的 `model`；可在当前会话中用原生 `/model` 临时切换，下一次 bot 重启则应用最新配置。面板也提供带二次确认的“重启 CLI”，Codex/Claude 都会恢复原 provider 会话与 transcript，保留上下文；Claude 模型卡额外提供“仅本会话”，避免修改未来新会话默认模型
- **@ 策略命令**:`/mention on` 表示无需 @，`/mention off` 表示必须 @，`/mention default` 恢复部署默认，`/mention status` 查看当前策略；设置按 binding 持久化且立即生效
- **按 route 选择 adapter**:同一 Telegram Bot/飞书 App credential 可按精确 topic/thread route 混合承载 Claude Code、Codex 和 Pi；不同 credential 仍可并行部署
- **核心命令**:`/status` `/info` `/whoami` `/new` `/resume` `/rename` `/esc` `/cc` `/eof` `/screen` `/restart` `/tmuxstop`；Pi 安装 `@narumitw/pi-plan-mode` 后支持 `/plan` 菜单及 `start/show/finalize/implement/save/export/exit` 子命令穿透
- **TUI 透传**:`/context` `/cost` `/usage` `/compact` `/clear` 等,抓屏结构化反馈
- **工具调用聚合**:一个 turn 内的 tool_use 流式刷同一条 IM 消息,真说话单独 push 触发通知
- **Codex 计划跟随**:`update_plan` 会维护一条可编辑的“当前计划”消息,TG/飞书里持续显示最新 `in_progress` / `pending` / `completed` 状态
- **双向附件**:Telegram/飞书收到的图片/文件会下载到本机并以 `@path` 注入 TUI;AI 回复里的绝对/相对路径、Markdown 文件链接和图片链接会转成原生 IM 附件,聊天内容不暴露服务器绝对路径
- **统一富消息**:Claude/Codex/Pi 共用 `ReplyDocument`;回复详细信息会显示运行时模型与档位（如 `gpt-5.6-terra medium`）；代码围栏可保留语言与 `filename=...` 标签，Markdown 表格在 Telegram 退化为对齐的原生 `<pre>` 数据块，在飞书使用 Card 2.0 根级 `table` 组件；Telegram 继续使用安全 HTML/可展开引用，飞书使用 header、summary、状态色和可选 CardKit 流式更新
- **长回复自动分页**:Telegram 按 HTML/UTF-16 安全边界拆成多条消息并保持代码块标签完整；飞书同时按 Card JSON 2.0 的 30KB payload 和每卡最多 50 个 body element 拆成连续卡片，不再因大量短 Markdown 段落触发 `element exceeds the limit`，也不把普通长回复截断成预览或强制改发 TXT
- **Telegram 状态标识**:Telegram 没有飞书式原生彩色卡片标题，使用 `🟡 工作中`、`🟠 等待输入`、`✅ 已完成`、`🔴 错误/阻塞`、`🔵 信息`、`⚪ 状态未知` 作为文本等价呈现
- **飞书状态色**:工作中黄色、等待输入橙色、完成/空闲绿色、错误/阻塞红色、普通信息蓝色、未知状态灰色；流式回复从黄色开始并在成功完成后变为绿色
- **picker 兜底**:claude TUI 事务式 flush jsonl 导致 picker 不可见时,屏幕 OCR 抓 picker 字符画推 inline keyboard
- **Pi 原生运行语义**:Working/streaming 时的普通文字、附件及 `/rename` pending 名称立即进入 Pi steering queue；CLI 从 shell 冷唤醒后必须观察到真实 Pi footer/status 才投递首条消息；自动压缩在 IM 中显示基于当前 session 历史中位耗时的可编辑倒计时，并只以 JSONL `type=compaction` 作为完成硬信号；当前 branch 的 `rpiv-todo` 快照持续显示在回复末尾，clear/全部 deleted 后隐藏；`pi-plan-mode` 的 active/ready/saved/implementing 状态与中文 widget 同步到 IM 页面底部，完整计划使用可编辑计划卡回推
- **活性指示**:TUI 状态行「时间 + token」指纹判活跃,工作中显示 typing(Telegram);飞书无 typing API
- **消息已读反应**:TG 👀 emoji(Bot API 7.0+);飞书 👀 OnIt reaction
- **订阅配额**:`/status` 展示 5h/7d 五窗口 utilization + 精确重置倒计时(走 OAuth API)
- **健壮性**:tmux paste 等 TUI idle 后提交，并读取 Claude/Codex/Pi 活动输入框确认；草稿仍在时有限重试 Enter，CLI 已工作或输入框已清空则停止。Telegram 最终回复逐分片校验 Bot API `message_id`，未确认送达时不推进 JSONL offset、下轮自动重试；tailer 另有 512KB 积压保护、GC 强引用和 offsets debounce。源码部署的 `bin/restart.sh` 会先重装当前 checkout 的 uv tool，避免 systemd 运行旧 wheel

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
     TUI idle 轮询 → paste-buffer
     → composer 渲染 → Enter → 确认/有限重试
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
make check-web   # 会先按 package-lock 执行 npm ci
# 发布机额外运行（会检查本机 tmux/CLI/运行目录）
make release-check
```

`main` 是 0.3.0 起的稳定基线和后续开发起点；发布点使用 `vMAJOR.MINOR.PATCH`
标签保存。产品边界、冻结能力和下一阶段维护债务见
[PRODUCTIZATION.md](./PRODUCTIZATION.md)。

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
- **M3** ✅ 接入 Codex CLI + 多 bot 共存 + systemd 部署
- **M4** ✅ 接入飞书前端(lark-oapi WebSocket + interactive card)+ 多实例支持
- **0.3.0** ✅ Runtime V2、中文 WebUI、XDG/doctor/systemd 安装面、终端接管与 TeamRun 基线

---

## License

[MIT](./LICENSE)
