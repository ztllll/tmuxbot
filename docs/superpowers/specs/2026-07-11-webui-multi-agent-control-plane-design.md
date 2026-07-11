# WebUI Multi-Agent Control Plane Design / WebUI 多 Agent 控制面设计

Date / 日期：2026-07-11 UTC

Status / 状态：Architecture and visual direction approved; implementation not started. / 架构与视觉方向已确认，尚未实施。

## 1. Goal / 目标

Build a single-user WebUI control plane for tmuxbot. It discovers and operates the host's tmux sessions, creates or resumes selected local AI CLI sessions for a project, and coordinates two or more role-specialized LLM agents without replacing tmux as the execution plane.

为 tmuxbot 建立单用户 WebUI 控制面：发现并操作宿主机 tmux 会话，为项目创建或恢复选定的本地 AI CLI，并统一调度两个或多个不同角色的 LLM Agent。tmux 始终是唯一执行面。

Default team / 默认团队：

- Coordinator / 调度统筹：拆解目标、生成依赖、汇总结果。
- Implementer / 代码执行：修改代码、运行测试、产出 diff 或 commit。
- Reviewer / 独立审查：只读审查、验证测试和验收证据。
- UI Designer / UI 设计：仅在 UI 任务中动态加入。

## 2. Scope / 范围

### V1 includes / V1 包含

- One Linux host and one authenticated human operator.
- Local access by IP and port; localhost is the secure default.
- Telegram, Feishu, and WebUI sharing runtime events, attachments, and provider adapters.
- Allowlisted CLI discovery and a project launch wizard.
- One tmux session per selected CLI, with native session resume.
- Role assignment, TeamRun scheduling, mailbox messages, artifacts, approvals, and history.
- Exact interactive terminals through xterm.js and PTY-backed `tmux attach-session`.
- Shared-directory single-writer scheduling.
- Architecture prepared for later Git worktree parallel writers.

### V1 excludes / V1 不包含

- Multi-user accounts, organizations, RBAC, billing, and public SaaS hosting.
- Multi-host federation.
- Headless SDK workers replacing interactive tmux CLIs.
- Full A2A or ACP protocol implementations.
- Unrestricted all-to-all natural-language agent chat.
- Multiple agents writing concurrently to one working tree.

## 3. Principles / 原则

1. Tmux is the execution source of truth. / tmux 是执行真相源。
2. Restart preserves native provider session identity and transcript. / 重启必须保留原生会话与 transcript。
3. The deterministic scheduler owns task state, dependencies, leases, retries, and acceptance gates. / 调度程序维护状态，不能只依赖调度 LLM 的上下文。
4. Agents exchange task envelopes and artifact references, not complete transcripts. / Agent 不广播完整上下文。
5. Agent completion claims are not acceptance evidence. / 自报完成不等于验收通过。
6. Browser terminal access is shell-equivalent and always authenticated. / Web 终端等同 Shell 权限，必须认证。

## 4. Architecture / 架构

```text
Web Browser                 Telegram                 Feishu
    │                           │                       │
    ▼                           ▼                       ▼
tmuxbot-web              TelegramFrontend       FeishuFrontend
    │                           │                       │
    └───────────────────────────┴───────────────────────┘
                                │
                    Shared Control Plane Core
              ┌─────────────────┼──────────────────┐
              │                 │                  │
       Session Registry   TeamRun Scheduler   Event/Artifact Store
              │                 │                  │
              └─────────────────┼──────────────────┘
                                │
                     Provider Adapter Contract
              ┌─────────┬───────┼─────────┬─────────┐
              │         │       │         │         │
           Claude     Codex   MiniMax    Grok     other CLI
              └─────────┴───────┴─────────┴─────────┘
                                │
                           tmux panes
                                │
                 Git workspace / worktrees / artifacts
```

`tmuxbot-web` runs as a separate service process. It shares core modules and persistence with the messaging runtimes, but a WebUI failure cannot stop Telegram, Feishu, or tmux sessions.

`tmuxbot-web` 独立运行并复用现有核心模块。WebUI 故障不得停止 Telegram、飞书或任何 tmux CLI。

## 5. Technology / 技术选型

Backend / 后端：

- Python 3.10+, FastAPI, and Uvicorn.
- REST for commands and queries.
- WebSocket for runtime events and binary terminal I/O.
- Standard-library PTY/subprocess primitives for tmux attachment.
- SQLite with a repository layer and numbered migrations.
- Reuse existing Runtime V2, backend adapters, event reducer, attachments, and session identity code.

Frontend / 前端：

- React, TypeScript, and Vite.
- xterm.js with fit, search, web-links, and clipboard support.
- A project-owned design system instead of a generic admin template.

## 6. Security / 安全

- Default bind address is `127.0.0.1`; LAN/Tailscale binding is explicit configuration.
- First-run single-user password setup with a modern password hash.
- Signed HTTP-only SameSite session cookie.
- CSRF protection for state-changing REST endpoints.
- Strict WebSocket Origin checks and short-lived terminal tickets.
- Browser values never become raw commands, paths, binaries, or tmux targets; all resolve through server-side allowlists.
- Killing sessions, deleting worktrees, stopping runs, merging, and deploying require confirmation and audit events.
- Direct unauthenticated public-port exposure is prohibited.

## 7. Domain model / 数据模型

- **Host**：V1 唯一宿主机、tmux socket、健康状态和已发现 Provider。
- **Project**：验证后的绝对项目路径、Git 信息、附件根目录和团队模板。
- **ProviderProfile**：CLI binary、版本探测、能力、启动/恢复、transcript、hooks 和命令。
- **AgentInstance**：Provider 会话、角色、tmux target、原生 session identity、workspace 和状态。
- **TeamTemplate**：角色到 Provider、权限和启动说明的可复用映射。
- **TeamRun**：一次统一执行，拥有任务图、Agent、审批、预算信号和时间线。
- **Task**：目标、约束、依赖、负责人、workspace 模式、尝试次数和验收条件。
- **Message**：用户、调度器和 Agent 之间的定向结构化邮箱消息。
- **Artifact**：计划、设计、文件、commit、diff、测试、截图、日志或审查报告。
- **Lease**：共享目录写锁或集成锁。
- **RunEvent**：追加式审计事件。

## 8. Provider adapter / Provider 适配器

Each adapter exposes capabilities instead of assuming universal commands:

```text
detect            version
create_session    resume_session
send_prompt       send_key
cancel_turn       compact_context
switch_model      find_transcript
parse_events      terminal_status
usage_snapshot    install_coordination_tools
```

Capability levels / 能力等级：

- L1 Terminal：启动、输入、按键、抓屏。
- L2 Session：会话身份、resume、cancel、model、compact。
- L3 Events：transcript/hooks、usage、结构化状态。
- L4 Coordination：MCP 或其他结构化 Agent 协议。

Unsupported capabilities are hidden or disabled. The scheduler never guesses provider commands.

## 9. Project launch / 项目启动

The wizard performs:

1. Validate project path and Git state.
2. Probe allowlisted CLI binaries with safe version commands.
3. Display version and capability level.
4. Select two or more providers.
5. Assign coordinator, implementer, reviewer, and optional designer roles.
6. Select a workspace mode supported by the current release; the first release exposes shared-directory mode, and the later worktree stage adds parallel mode to the same wizard.
7. Create or attach collision-safe tmux sessions.
8. Resume saved provider sessions.
9. Validate hooks or Coordinator MCP only after confirmation.
10. Persist all roles, targets, identities, and capabilities.

Finding a binary on PATH does not authorize its execution by itself.

## 10. Scheduling / 调度

### Default collaboration / 默认协作

- Coordinator proposes the task DAG.
- The program validates cycles, dependencies, owners, and artifacts.
- Read-only research and reviews may run concurrently.
- Shared-directory write tasks obtain one write lease.
- Reviewer starts only after implementation evidence exists.
- Acceptance checks run independently from worker self-report.
- Retries and reassignment are bounded.
- Stagnation moves the run to `operator_required`.

### Worktree parallel mode / Worktree 并行

- Independent write tasks receive separate branches and worktrees.
- Each writer produces a commit and validation evidence.
- Integration uses a serialized merge queue and integration lease.
- Conflicts, destructive Git actions, and deployment remain human-gated.

### Brainstorm mode / 头脑风暴

- All agents start read-only with the same compact brief.
- First-round answers are hidden from peers to reduce anchoring.
- Second round exchanges summaries and explicit objections.
- Coordinator produces a conflict matrix and synthesis artifact.
- No code write starts before operator approval.

## 11. Communication / 通信

Preferred transport is a project-owned Coordinator MCP server. Initial tools:

```text
list_tasks          claim_task
get_task_context    report_progress
send_message        publish_artifact
request_review      report_blocked
complete_task
```

Every mailbox item contains `run_id`, `task_id`, sender, recipient, goal, constraints, dependency references, artifact references, status, attempt, and evidence.

For CLIs without MCP, the adapter injects a bounded task envelope into the TUI and extracts results from transcript/hooks or an explicit artifact. The central mailbox remains authoritative. Agents never send unrecorded text directly to another pane.

## 12. Context governance / 上下文治理

Three layers:

1. Provider-private native session and transcript.
2. TeamRun tasks, mailbox, decisions, and artifact references.
3. Durable project rules in `CLAUDE.md`, `AGENTS.md`, specs, and conventions.

Rules:

- Never broadcast full transcripts.
- Prefer file, commit, line-range, and artifact references.
- Before provider-native compaction, save a checkpoint artifact with goal, completed work, open decisions, changed files, evidence, and next action.
- Compact only through the adapter.
- Verify session identity and transcript continuity after compact or restart.
- Coordinator context may rotate without losing durable TeamRun state.

## 13. Acceptance and recovery / 验收与恢复

A task reaches `accepted` only when configured evidence passes: required files, scoped diff/commit, successful tests/lint, no unresolved blocking review findings, UI screenshots/interactions, or deployment health evidence.

```text
pending → ready → assigned → working → review → accepted
                                  ├→ blocked
                                  ├→ failed → retrying
                                  └→ operator_required
```

The scheduler detects repeated messages, unchanged Git state, repeated command failures, missing artifacts, inactive panes, quota exhaustion, and context pressure.

After service restart, it reconciles SQLite with tmux sessions, pane commands, worktrees, and provider transcripts. Orphaned sessions are surfaced for attach/adopt/archive and are never killed automatically.

## 14. Web terminal / Web 终端

- Each browser terminal creates a PTY that attaches to a registered tmux target.
- Tmux handles rendering and multi-client synchronization.
- WebSocket carries binary terminal output and resize messages.
- Default mode is observe-only; entering control mode is visible and audited.
- The terminal supports single pane, tabs, grid, docked, and full-screen layouts.
- Browser disconnect closes only the PTY client, never the tmux session.
- Reconnection attaches to the same target.

## 15. Information architecture / 信息架构

- `/` 调度指挥台
- `/projects` 项目与启动向导
- `/runs/:id` 任务图和运行时间线
- `/agents/:id` Agent、上下文、配额和终端
- `/artifacts` diff、测试、文件、截图和报告
- `/history` 运行回放和审计
- `/settings` Provider、团队模板、认证和通道设置

```text
┌────────────────────────────────────────────────────────────┐
│ Host health · Project · TeamRun state      New team · Stop │
├────────────┬────────────────────────────┬──────────────────┤
│ Projects   │ Task graph + Run Spine     │ Agent formation  │
│ and runs   │ dependencies and evidence  │ status and quota │
├────────────┴────────────────────────────┴──────────────────┤
│ Resizable terminal dock: one pane, tabs, or terminal grid │
└────────────────────────────────────────────────────────────┘
```

## 16. Visual system / 视觉系统

The UI is a light industrial control surface around native dark terminals. It avoids generic dark-neon hacker dashboards and avoids wrapping every region in rounded cards.

### Palette / 配色

- Cloud Steel `#E7EBEF` — 主背景。
- Ink `#17212B` — 文字和结构线。
- Circuit Blue `#3157C8` — 选择、路由和信息。
- Work Amber `#D59620` — 执行中和等待。
- Outcome Green `#18866B` / Fault Red `#C84848` — 完成与错误。

### Typography / 字体

- IBM Plex Sans：标题和核心数字。
- Noto Sans SC：中文正文与控件。
- IBM Plex Mono：命令、路径、模型、token 和 ID。

### Signature / 标志性元素

The Run Spine is a live routing rail connecting the requirement, delegated tasks, agents, review gates, and operator approvals. Node color and labels always represent real state.

Run Spine 是贯穿任务区的运行脊柱，将需求、Agent、依赖、审查、测试和人工批准连接起来。周边界面保持克制，让这一元素成为唯一明显的视觉记忆点。

### Interaction / 交互

- Chinese-first labels; provider names and commands stay native.
- Direct action verbs: `暂停任务`, `重新分配`, `打开终端`.
- Errors state cause and recovery action.
- Status never depends on color alone.
- Visible focus, keyboard navigation, and reduced-motion support.
- Motion is limited to task transitions, attention signals, and terminal expansion.
- Mobile shows one selected terminal with a compact agent switcher.

## 17. Telegram and Feishu / 通道联动

- WebUI is the full control plane; Telegram and Feishu remain lightweight remote controls.
- Notifications identify the exact TeamRun, task, agent, and artifact.
- Channel actions call the same scheduler APIs and cannot bypass leases or acceptance gates.
- Agents' files and images remain deliverable as native channel attachments.
- Approval, pause, resume, stop, and summarized status are exposed through explicit channel commands or panels.

## 18. Delivery stages / 交付阶段

1. Domain models, SQLite, authentication, and read-only tmux inventory.
2. Command Center shell, visual system, live events, and terminal attach.
3. Project wizard, CLI discovery, ProviderProfile, and tmux provisioning/resume.
4. TeamRun scheduler with coordinator, implementer, reviewer, write lease, mailbox, and artifacts.
5. Telegram/Feishu notifications and approvals.
6. Acceptance gates, replay, recovery, context checkpoints, and provider compaction controls.
7. Worktree parallel mode, merge queue, and integration lease.
8. Dynamic UI designer, brainstorm mode, adaptive routing, and additional providers.

Each stage must be independently testable and preserve existing Telegram, Feishu, tmux, and provider-session behavior.

## 19. Verification / 验证

- Unit tests for transitions, DAG validation, leases, retries, capability routing, and recovery.
- Shared adapter contract tests for every provider.
- SQLite migration and crash-recovery tests.
- REST/WebSocket auth, Origin, CSRF, and terminal-ticket tests.
- PTY/tmux integration tests with disposable sessions.
- Browser tests for launch wizard, Command Center, task changes, terminal reconnect, keyboard navigation, and mobile layout.
- Visual regression screenshots for approved tokens and screens.
- Two-agent E2E: coordinator dispatches, implementer produces evidence, reviewer validates, scheduler accepts.
- Restart E2E: Web service restarts while tmux continues, then reconciles without losing transcript or task state.
- Deployment verification preserves local and hbhy tmux session counts and all existing channel bindings.

## 20. Approved decisions / 已确认决策

- Native tmuxbot WebUI, not an embedded third-party dashboard.
- Single-user and single-host first release.
- Unified scheduling of two or more LLM CLIs.
- Coordinator + implementer + reviewer by default; UI designer joins dynamically.
- Shared-directory single-writer safety with planned worktree parallelism.
- Interactive tmux CLI execution remains primary; no SDK/API workers in V1.
- Structured mailbox and artifacts; no unrestricted pane-to-pane chat.
- Command Center home with expandable xterm.js terminal dock.
- Light industrial visual system with Run Spine as the signature element.
