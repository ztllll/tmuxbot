# tmuxbot 零配置启动、Provider 向导与输入可靠性设计

日期：2026-07-11 UTC

状态：方向已确认，等待用户审阅书面规格；尚未实施。

## 1. 目标

将 tmuxbot 从“源码目录中的 Telegram/飞书桥接程序”推进为单用户本地控制设备：一条命令安装，一条命令运行，首次启动后在 WebUI 内完成 Provider、项目、通道、binding 和 tmux 会话配置。

第一版标准入口：

```bash
uv tool install 'tmuxbot[full]'
tmuxbot serve --open
```

本阶段同时修复公共 tmux 输入运行时中的 post-paste 提交竞态，确保飞书、Telegram、Claude、Codex 的多行附件提示能够稳定提交。

## 2. 用户体验里程碑

### Preview A：可打开的本地调度台

完成 Z1 和 Web 前端壳后即可体验：

- 单命令安装与运行。
- 首次密码设置。
- 系统、tmux 和数据目录健康状态。
- 已发现 CLI 候选、版本与路径。
- 当前 tmux session 清单。
- 尚未提供直接 TUI 操作或多 Agent 调度。

### Preview B：可用的单 CLI 控制台

完成 Z2 与终端最小闭环后即可体验：

- WebUI 创建或接管受管 tmux CLI 会话。
- 被动 Provider 检查。
- 用户明确触发的“测试回复”。
- 会话级模型切换及结果验证。
- xterm.js 单终端观察和显式接管。
- 项目、Provider、通道和 binding 在 WebUI 中配置。

### Preview C：首个多 LLM 协作闭环

完成 TeamRun 最小调度器后即可体验：

- Coordinator、Implementer、Reviewer 三角色。
- 两个或多个真实 tmux CLI 会话。
- 任务 DAG、结构化邮箱、Artifact 和共享目录写租约。
- Implementer 产出证据，Reviewer 独立审查，程序验收。
- 第一版只支持共享目录单写者，不开放并行写入。

## 3. 范围拆分

### Z0：飞书图片和多行输入可靠性热修复

当前 `TmuxRuntime.send_text()` 在 bracketed paste 完成后立即发送 Enter。tmux 子进程退出只能证明数据写入 PTY，不能证明 Claude/Codex TUI 已处理完 paste 事件。多行附件提示因此可能停留在输入框。

修复：

```text
等待 pane 可输入
→ 校验前台进程
→ bracketed paste
→ 等待 post_paste_delay（默认 0.5 秒）
→ Enter
```

约束：

- `with_enter=False` 不等待、不发送 Enter。
- delay 位于 per-target 输入锁内部，下一条消息不得提前 paste。
- 不在飞书 handler 额外补 Enter。
- Telegram、飞书、普通文本和附件统一走同一运行时语义。
- 保留 paste 前的 busy 等待和 foreground 校验。

### Z1：可安装、可零配置启动

交付：

- `full` extra，包含 Web、飞书和完整运行依赖。
- XDG 路径：
  - 配置：`~/.config/tmuxbot/`
  - 数据：`~/.local/share/tmuxbot/`
  - 状态与日志：`~/.local/state/tmuxbot/`
- `tmuxbot serve --open`。
- 空 binding、空通道、空 Provider 配置下 Web 仍可启动。
- Web 与 bridge 分离生命周期；bridge 配置不可运行时显示“尚未配置”，不终止 Web。
- 首次 setup secret 自动生成、短期有效，只显示在当前本机终端或一次性本机 URL 中，不要求用户预先编辑 `.env`。
- `tmuxbot doctor` 检查 Python、tmux、路径、数据库和 Provider 候选。
- 保留 `tmuxbot bridge`、`tmuxbot web` 和旧源码部署兼容入口。

不在 Z1 交付：完整 React 调度台、TUI 终端、多 Agent 调度。

### Z2：WebUI Provider、项目和配置向导

Provider 合同新增：

```text
detect_candidates
probe_binary
build_launch_argv
build_resume_argv
probe_reply
switch_model
verify_model
terminal_status
```

#### CLI 扫描

- 第一版 allowlist：`tmux`、`claude`、`codex`。
- PATH 发现只产生候选，不自动授权执行。
- 使用 argv 数组、无 shell、3 秒超时、64 KiB 输出限制。
- 保存 realpath、版本、inode 和 mtime；实际启动前重新验证 binary identity。
- 用户可选择已发现候选或输入绝对路径；必须是 regular executable。

#### 回复探测

- 被动探测不产生模型费用：版本、启动能力、TUI readiness、transcript/hook 能力。
- 主动探测由用户点击触发，并提示可能消耗额度。
- 主动探测只使用 tmuxbot 创建并登记 ownership 的测试 session。
- 注入 nonce prompt，同时验证 TUI 状态、provider session identity 和 transcript/hook 中对应回复。
- 超时或证据不完整返回 `unknown/failed`，不返回假成功。

#### 模型切换

能力拆分：

- `launch_model_override`
- `live_session_model_switch`
- `model_enumeration`
- `model_verification`
- `session_only_switch`
- `persistent_default_switch`

第一版只承诺新会话模型指定和“仅本会话”的 live switch。模型切换必须通过 Provider 状态或 `/status` 结果验证，不能把按键成功当作模型切换成功。

#### 配置权威源

新安装以 SQLite 为配置权威源，新增：

- `provider_profiles`
- `channel_profiles`
- `projects`
- `bindings`
- `managed_sessions`
- `probe_results`
- `config_revisions`

敏感凭据进入独立 `0600` secret store，数据库只保存引用和掩码状态。API 不返回完整 secret。

旧 `.env` 和 `bindings.yaml` 只提供一次性导入：预览、用户确认、导入、停止双向写入。过渡期可生成只读 legacy snapshot，但 WebUI 与通道不得分别修改两个权威源。

### Z3：Web TUI 与常驻服务

最小终端：

- xterm.js 单 pane。
- PTY 只执行参数数组形式的 `tmux attach-session -t <server-resolved-target>`。
- 默认 observe-only。
- 显式 takeover 后才能发送键盘输入。
- takeover 时暂停 bridge/scheduler 对同一 target 的注入。
- 浏览器断开只关闭 attach client，不终止 tmux session。
- terminal ticket 单次、短期、绑定登录 session 和服务端 session ID。
- WebSocket 严格 Origin、输入输出限流、接管审计。

常驻入口：

```bash
tmuxbot install-service --now
```

命令根据当前 console executable 和 XDG 路径生成 systemd user service，运行 `tmuxbot serve`，不得把密码、token 或 setup secret 放入 `ExecStart`。

### Z4：TeamRun 多 LLM 最小闭环

第一版角色：

- Coordinator：提出任务 DAG 和上下文包。
- Implementer：唯一写者，修改代码并产出测试与 diff/commit Artifact。
- Reviewer：只读审查证据。

调度器负责：

- DAG 环检测和依赖状态。
- Agent assignment。
- 共享目录写租约。
- 结构化 mailbox。
- Artifact 和 evidence。
- bounded retry、blocked、operator-required。
- 独立 acceptance gate。

Agent 自报完成只能进入 review，不能直接 accepted。第一版不开放多个 Agent 同时写一个目录；worktree 并行属于后续阶段。

## 4. `serve` 架构

```text
tmuxbot serve
  ├─ Web control plane（始终运行）
  ├─ Config repository / secret store
  ├─ Provider discovery and probes
  └─ Bridge supervisor
       ├─ Telegram child/runtime（配置有效时）
       └─ Feishu child/runtime（配置有效时）
```

Web 故障、bridge 重启和 Provider probe 均不得终止 tmux session。飞书 SDK 的多实例/event-loop 限制继续通过独立 bridge child process 隔离。

## 5. WebUI 信息架构

Z1/Z2 第一版中文优先页面：

- `/setup` 首次设置。
- `/` 系统状态与下一步引导。
- `/providers` CLI 扫描、版本、能力、被动/主动探测。
- `/projects` 项目验证和默认团队。
- `/channels` Telegram、飞书凭据与连接测试。
- `/sessions` tmux 会话、模型、回复探测。
- `/terminal/:id` 单 TUI 终端。
- `/settings` 服务、路径、安全和迁移状态。

每个操作显示：做什么、是否消耗模型额度、是否修改 tmux/Git/配置、成功证据和恢复方法。

## 6. 安全约束

- 默认只监听 `127.0.0.1`。
- PATH 发现不等于执行授权。
- 浏览器值只能引用服务端已验证 ID，不能直接变成 binary、path、tmux target 或 shell command。
- Provider probe 和 terminal attach 使用固定 executable 与 argv，无 shell。
- 主动回复探测可能消耗额度，必须显式触发。
- 只清理由 tmuxbot 创建并登记 ownership 的测试 session。
- 最高权限启动参数必须在 UI 明示，不能作为隐藏默认值。
- secret 不进入 URL query、命令行、RunEvent、日志或普通 API 响应。
- 配置 revision 变更可重启 bridge，但不能 kill tmux session。

## 7. 测试与验收

### Z0

- RED：paste 后立即 Enter 的 fake TUI 丢失提交。
- GREEN：顺序为 `inspect → paste → sleep(0.5) → Enter`。
- `with_enter=False` 无 delay/Enter。
- 并发消息在完整提交后才释放锁。
- Claude/Codex 多行附件 prompt 均覆盖。

### Z1

- 空 HOME、无 `.env`、无 bindings：安装后 `tmuxbot serve` 成功。
- WebUI 可以完成首次设置。
- `doctor` 输出清晰状态且不泄漏 secret。
- Web 保持健康，bridge 显示未配置。
- wheel 包含 Web 静态资源。

### Z2

- Claude/Codex 使用同一 Provider contract test。
- binary timeout、输出限制、symlink/identity 替换和未授权候选测试。
- 被动 probe 不产生模型调用。
- 主动 reply probe 具备 nonce、超时、session ownership 和 evidence。
- 模型切换失败不得更新成功状态。
- legacy 配置导入后只有一个权威配置源。

### Z3

- terminal ticket 过期、重放、错误 session、错误 Origin 拒绝。
- observe 模式不能写入。
- takeover 审计和输入互斥。
- 浏览器断开后 tmux session 仍存在。

### Z4

- Coordinator → Implementer → Reviewer → accepted 两 Agent E2E。
- 写租约阻止并发 writer。
- Agent 自报完成但无 evidence 时不能 accepted。
- Web/bridge 重启后任务和 tmux session 可恢复。

## 8. 交付顺序与体验节点

```text
Z0 输入热修复
  ↓
Z1 单命令安装/运行 + 零配置 Web bootstrap
  ↓  Preview A：可打开的调度台
Z2 Provider/项目/通道/会话向导
  ↓
Z3 xterm 单终端 + takeover
  ↓  Preview B：可用的单 CLI 控制台
Z4 TeamRun 调度器
  ↓  Preview C：首个多 LLM 协作闭环
后续：通道投影、恢复回放、worktree 并行、brainstorm mode
```

## 9. 当前完成度与剩余工作

已完成：

- Runtime V2 的 Telegram/飞书通道基础。
- Codex/Claude Provider 事件、session identity 和 transcript 适配基础。
- 附件原生收发和长消息分段。
- Web Phase 1 后端：认证、SQLite、RunEvent、只读 tmux inventory、独立 Web 服务入口和 systemd user service。

尚未完成：

- Z0 post-paste 输入热修复。
- 可发布 wheel 的 `full` 安装体验。
- `serve` supervisor、XDG 路径和零 binding bootstrap。
- React/Vite WebUI 页面与静态资源打包。
- Provider scan/probe/model verification 正式合同。
- WebUI 项目、通道、binding 和 secret 配置。
- SQLite 配置权威源及 legacy 导入。
- PTY/WebSocket/xterm.js TUI。
- TeamRun DAG、mailbox、Artifact、lease、acceptance gate。
- Coordinator MCP、上下文 checkpoint 和 compaction 治理。
- Telegram/飞书 TeamRun 通知与审批投影。
- worktree 并行、merge queue 和 brainstorm mode。

## 10. 已确认决策

- 第一版安装入口采用 `uv tool install 'tmuxbot[full]'`。
- 第一版运行入口采用 `tmuxbot serve --open`。
- 容器不作为主交付方式。
- 先完成 Z1 零配置闭环，再扩展完整 Command Center。
- Provider 主动探测和模型切换必须有真实证据，不能猜测成功。
- tmux 始终是执行真相源。
