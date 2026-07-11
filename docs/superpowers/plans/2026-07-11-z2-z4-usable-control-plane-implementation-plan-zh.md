# Z2–Z4 可用控制面实施计划

> 按 `superpowers:subagent-driven-development` 执行；每个任务 RED → GREEN → review → 集成。

**目标：** 在 Z1 零配置启动之上，交付可从中文 WebUI 扫描 Claude/Codex、登记项目和受管 tmux 会话、查看/接管真实 TUI，并运行 Coordinator → Implementer → Reviewer 的首个确定性 TeamRun。

**边界：** tmux 始终是 CLI 执行面；浏览器只提交服务端 ID；命令均为 argv 数组且不用 shell；V1 单用户、单宿主机；TeamRun 第一版只允许一个共享目录写者。

## Task 1：Provider 与项目配置存储

- 扩展 SQLite migration：`provider_profiles`、`projects`、`managed_sessions`、`probe_results`。
- Repository 提供类型化 CRUD；保存 realpath、版本、inode、mtime，不保存任意 shell 字符串。
- 测试空库升级、已有 v1 库升级、唯一约束、路径/secret 脱敏。

## Task 2：Allowlist CLI 扫描与被动探测

- 新增 `tmuxbot/providers/discovery.py`，只扫描 `tmux`、`claude`、`codex`。
- 使用 `shutil.which`、realpath、regular executable 校验、3 秒 timeout、64 KiB 输出上限。
- API：`GET /api/providers`、`POST /api/providers/scan`、`POST /api/providers/{id}/probe`。
- 未登录、CSRF/Origin、未知 binary ID、TOCTOU identity 变化均拒绝。

## Task 3：项目与受管 tmux 会话

- API：项目校验/CRUD、创建会话、登记已有会话、会话列表。
- cwd 必须是服务端验证过的绝对目录；tmux 名称由服务端生成并检查碰撞。
- Claude/Codex launch/resume 使用 Provider adapter 的 argv，再经安全 tmux command rendering。
- WebUI 提供中文扫描、项目、Provider、会话向导及明确能力/费用提示。

## Task 4：Web 终端 observe/takeover

- 新增一次性短期 terminal ticket，绑定 Web session 与 managed-session ID。
- WebSocket 严格 Origin；PTY 只运行 `tmux attach-session -t <resolved target>`。
- 默认 observe-only；takeover API 开启后才能输入；断开只关闭 attach client。
- xterm.js 单终端、resize、重连提示；记录 takeover start/end RunEvent。

## Task 5：TeamRun 领域模型与持久化

- migration：`team_runs`、`team_agents`、`team_tasks`、`mailbox_messages`、`artifacts`、`write_leases`。
- 实现 DAG 环检测、依赖 readiness、角色能力校验、单写租约、幂等事件。
- 状态机禁止 worker 自报直接 accepted；必须进入 review 并由独立 Reviewer/程序验收。

## Task 6：Tmux TeamRun 调度器

- Coordinator 生成/提交受验证任务图；第一版也支持用户在 WebUI 直接填写确定性三角色模板。
- Scheduler 向已登记 tmux pane 注入结构化上下文包，观察现有 transcript/status 证据。
- Implementer 完成后生成 diff/test/commit Artifact；Reviewer 收到只读审查包并返回 verdict。
- bounded retry、blocked、operator-required、pause/resume/stop；重启 reconciliation 不重复派发。

## Task 7：中文 TeamRun UI 与统一事件

- Command Center 展示 Agent、任务依赖、租约、事件时间线和 Artifact。
- API 支持创建、启动、暂停、恢复、停止、提交证据、review verdict。
- Telegram/飞书至少能收到 TeamRun 状态摘要；通道失败不阻塞持久化。

## Task 8：安装、服务与端到端验收

- `tmuxbot install-service --now` 生成 systemd user unit，运行 `tmuxbot serve`。
- 构建 wheel；空 HOME 启动；真实 `tmux/claude/codex --version` 被动探测。
- fake CLI E2E：两 Provider 会话、terminal attach、TeamRun implement → review → accepted。
- 真实主机 smoke：只做无费用版本/启动 readiness；任何会消耗模型额度的主动回复测试必须由显式测试开关启用。
- 最终运行 `make check`、前端测试/build、wheel E2E、`git diff --check`，更新中英文用户文档并推送。

