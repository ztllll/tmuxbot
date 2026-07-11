# WebUI 多 Agent 控制面实施路线图

> **Agent 执行要求：** 每个阶段必须使用 `superpowers:subagent-driven-development`（推荐）或 `superpowers:executing-plans` 按对应实施计划逐项执行；所有步骤使用复选框跟踪。

**目标：** 在不替换 tmux、不破坏现有 Telegram/飞书 Runtime V2 的前提下，逐步交付单用户 WebUI、多 CLI 会话管理和确定性多 Agent 调度。

**架构：** WebUI 作为独立进程复用 tmuxbot 的核心契约、SQLite 状态库和追加式 `RunEvent`。tmux 继续保存交互式 CLI 进程和原生上下文，调度器只管理任务、租约、邮箱、证据与验收。

**技术栈：** Python 3.10+、FastAPI、Uvicorn、SQLite、React、TypeScript、Vite、xterm.js、pytest、Vitest、Playwright。

## 全局约束

- tmux 是唯一执行面，Web 服务重启不得终止 tmux 会话。
- 默认监听 `127.0.0.1`，非本机监听必须显式配置。
- V1 单用户、单宿主机；所有 Web 终端和写操作必须认证。
- WebUI、Telegram、飞书和通知只消费持久化 `RunEvent`。
- 不覆盖用户全局 Codex、Claude、MCP、`CLAUDE.md` 或 `AGENTS.md` 配置。
- Provider 不支持的能力必须禁用，禁止猜测 CLI 命令。
- 共享目录同时只允许一个写任务；并行写入必须使用独立 worktree。
- 每阶段独立可测试、可回滚，并保持现有通道、绑定、tmux target 和 provider session identity。

---

## 阶段划分

### Phase 1：控制面基础

交付：Web 配置、SQLite 迁移、核心领域模型、追加式 `RunEvent`、单用户认证、只读 tmux 清单和孤儿会话分类。

实施计划：`docs/superpowers/plans/2026-07-11-webui-control-plane-foundation-implementation-plan-zh.md`

验收门槛：`tmuxbot web` 可独立启动；未登录无法读取会话；读取清单不会创建、注入或杀死任何 tmux 会话；重启后事件和认证状态可恢复。

### Phase 2：Command Center 与终端

交付：React 应用壳、Run Spine 视觉系统、桌面 Command Center、移动端遥控布局、WebSocket 事件流、xterm.js 观察模式与审计接管模式。

主要边界：`tmuxbot/web/terminal/` 只处理 PTY attach、resize 和 ticket；`webui/src/features/terminal/` 不持有调度状态；终端接管时调度器停止向该 target 注入按键。

验收门槛：浏览器断开只关闭 PTY 客户端；tmux 会话继续运行；Origin、ticket、observe/control 权限和接管审计测试全部通过。

### Phase 3：项目启动与 Provider Profile

交付：项目路径验证、allowlist CLI 探测、能力矩阵、项目向导、碰撞安全的 tmux 创建/接管、Codex/Claude 原生会话恢复。

主要边界：Provider Profile 返回结构化命令参数，不返回 shell 字符串；浏览器提交的 binary、cwd、tmux target 必须解析为服务端已验证 ID。

验收门槛：Codex 与 Claude contract test 共用同一套场景；未知进程和未授权 binary 无法启动；旧会话身份和 transcript 连续性保持。

### Phase 4：TeamRun 确定性调度

交付：任务 DAG、角色分配、共享目录写租约、结构化邮箱、Artifact、重试、阻塞与 operator-required 状态、独立验收门。

主要边界：协调 LLM 只能提出 DAG；程序验证环、依赖、owner、lease 和 evidence；Agent 自报完成只进入 review，不直接 accepted。

验收门槛：两 Agent E2E 中 implementer 产出证据、reviewer 审查、程序验收；重复消息、租约超时和服务重启不会重复执行写任务。

### Phase 5：Telegram/飞书统一投影

交付：TeamRun 状态通知、审批、暂停、恢复、停止、Artifact 原生附件；TG 与飞书使用相同 `RunEvent` projection 和 scheduler command service。

主要边界：通道按钮和 `/` 命令不能绕过租约、确认或验收门；长文本继续分段，文件和图片继续作为原生附件发送。

验收门槛：同一个 `RunEvent` 在 WebUI、TG、飞书呈现相同业务状态；通道故障不会阻塞事件持久化和其他通道。

### Phase 6：恢复、回放与上下文治理

交付：时间线回放、服务重启 reconciliation、checkpoint artifact、provider-native compact、配额/上下文压力、孤儿 adopt/archive/ignore。

主要边界：压缩前先保存目标、已完成事项、决策、文件、证据和下一步；协调 Agent 可轮换但 TeamRun 状态不能丢失。

验收门槛：Web 服务重启期间 tmux 和通道继续独立存活；恢复后不会丢失任务、事件、transcript identity 或待审批动作。

### Phase 7：Worktree 并行与集成队列

交付：独立分支/worktree、`.worktreeinclude`、限时 setup/teardown hook、FileClaim 预检、实际 diff 冲突验证、串行 merge queue、端口和进程清理。

主要边界：FileClaim 只做预警；最终集成以真实 changed paths 为准；不复制完整 ignored tree；只清理由 tmuxbot 记录所有权的端口和进程。

验收门槛：两个独立写任务可并行；重叠 claim 被序列化；未声明 diff 阻止集成；冲突和破坏性 Git 操作必须人工确认。

### Phase 8：动态团队与头脑风暴

交付：动态 UI Designer、隐藏首轮的 brainstorm、异议矩阵、团队模板、更多 Provider、基于能力与成本信号的建议路由。

主要边界：路由结果是建议，不自动扩大 Provider 权限；头脑风暴阶段全部只读，人工批准前不进入代码写入。

验收门槛：首轮答案互不可见；第二轮只交换摘要和异议；最终 synthesis 保留来源和冲突，且无法绕过 operator gate。

## 实施顺序

```text
Phase 1 → Phase 2 → Phase 3 → Phase 4 → Phase 5 → Phase 6 → Phase 7 → Phase 8
             └──────── 终端能力 ────────┘        └──── 恢复基础 ────┘
```

Phase 1–4 构成首个可用闭环；Phase 5 将闭环扩展至现有通道；Phase 6–7 完成长期运行和安全并行；Phase 8 才引入更开放的协作方式。

## 每阶段统一发布检查

- [ ] 运行 `make check`，预期 compile、pytest、ruff 全部通过。
- [ ] 运行该阶段新增的集成和浏览器测试，预期全部通过。
- [ ] 对 SQLite migration 做空库升级和已有库升级测试。
- [ ] 对照正式规格逐项检查安全、恢复和通道兼容要求。
- [ ] 确认 `git diff --check` 无空白错误。
- [ ] 提交一个可独立回滚的阶段性 commit，并推送当前分支。
