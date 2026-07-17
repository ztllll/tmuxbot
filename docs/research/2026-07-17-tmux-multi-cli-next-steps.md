# tmux 多 CLI 协作：下一阶段架构、可观测性与调度模式

日期：2026-07-17
范围：研究结论，不授权引入第三方运行时或修改产品代码。

## 结论先行

tmuxbot 不应把 tmux/CLI 替换为 API Agent 框架。它已经拥有正确的执行面：每个
受管 CLI 都是可保留上下文的真实本地会话，`TeamRun` 也已经有持久任务、DAG、mailbox、
artifact、写租约、重试和恢复。下一阶段应把它强化成一个**以数据库事件为事实来源、以
tmux 为执行器、以通道/Web 为投影与人工控制面**的工作流内核。

最值得借鉴的不是某个“多 Agent UI”，而是四个成熟边界：

1. 把“发给 CLI”“CLI 已接收”“人/CLI 已确认完成”分成可恢复的命令生命周期；
2. 把动态统筹限制为生成/修订结构化计划，真正的状态迁移仍由确定性调度器校验；
3. 将事件、终端状态和通道操作关联为一次可追踪的运行，而不是只靠终端屏幕文本；
4. 对长任务采用租约、超时、幂等键、暂停/恢复与人工介入，而不是无限重发。

## 当前内核与缺口

当前 `TeamRun` 已具备很好的最小内核：三角色、每任务依赖、单写租约、持久 mailbox、
artifact、重试和 `reconcile`；`TerminalService` 又将“观察”和“接管输入”分开。缺口主要
不在增加更多模型，而在以下运行语义尚未成为一等对象：

| 已有能力 | 下一步补齐的语义 |
| --- | --- |
| `TeamTask` / `RunEvent` | command、dispatch receipt、attempt、heartbeat、timeout、人工决策的不可变关联 |
| 直接向 tmux 发送任务 | outbox + 发送确认 + 幂等重投；避免进程重启时“已写 DB、未送达”或反向情况 |
| 单写租约 | 租约 owner、过期、心跳、抢占规则，以及 pane/CLI 已退出时的明确 `operator_required` |
| Web/TG/飞书显示状态 | 同一事件流的投影；每条状态能回链到任务、会话、pane、attempt 与操作人 |
| Coordinator 角色 | 仅可提出计划/重新规划，不能绕过能力、依赖、租约和审查规则直接改状态 |

## 可直接借鉴的模式

### 1. 采用“命令、查询、异步信号”三分法

[Temporal 的工作流消息模型](https://docs.temporal.io/encyclopedia/workflow-message-passing)
区分只读 Query、异步 Signal 和可等待结果的 Update；其
[消息处理指南](https://docs.temporal.io/handling-messages)强调幂等键、输入校验和在工作流
结束前处理完 handler。tmuxbot 不需要引入 Temporal，也可借其语义：

| tmuxbot 操作 | 推荐语义 |
| --- | --- |
| Web/TG/飞书查看运行、pane、模型、日志 | Query：只读，不产生状态事件 |
| CLI 屏幕/JSONL 发现“任务可能完成”、心跳、pane 死亡 | Signal：异步事实，写入观测事件，不能直接视为完成 |
| 暂停、恢复、停止、接受计划、接管终端、登记 artifact、审查 verdict | Update：同步校验、稳定 idempotency key、返回被接受/拒绝的理由 |

这会消除“UI 看上去成功但调度器尚未接受”的歧义。每个 Update 先经过 policy validator，
再追加 `RunEvent`，最后由投影更新 Web、Telegram、飞书卡片。对长运行任务，可参考
[Temporal 的事件历史与 replay/恢复说明](https://docs.temporal.io/workflow-execution)：记录每次
状态转换，重启后从已持久化事实继续，而不是重新推断整段终端历史。

### 2. 将 tmux 发送改为 Transactional Outbox + 回执状态机

AutoGen Core 的
[消息与通信文档](https://microsoft.github.io/autogen/stable/user-guide/core-user-guide/framework/message-and-communication.html)
明确区分紧耦合的 direct request/response 与单向 publish/subscribe。tmuxbot 的实现应保持
本地、轻量，但可采用相同边界：

```
Task ready
  -> transaction: task=assigned + OutboxCommand(created)
  -> sender: tmux_send_text
  -> receipt: accepted / screen-observed / jsonl-observed / failed
  -> task=working 或 operator_required
```

- `OutboxCommand` 应有 `command_id`、`run_id`、`task_id`、`attempt`、目标 managed session、
  payload digest、created/sent/acknowledged/expired 时间和失败分类。
- 发送器只负责 side effect；调度器只消费已持久化 outbox。崩溃恢复时按 command id 重投或
  标记不确定，绝不靠“再发一遍自然语言”猜测。
- CLI 无法提供可靠 ACK 时，把回执分层：`tmux_written`（已注入）、`prompt_visible`、
  `provider_event_observed`、`operator_confirmed`；UI 必须显示证据等级。

尤其要区分**可安全重试**与**执行结果不确定**。Temporal 的
[重试策略](https://docs.temporal.io/encyclopedia/retry-policies)和
[错误处理建议](https://docs.temporal.io/best-practices/error-handling)说明，外部 Activity 的失败
需要有明确重试策略与非重试错误。对应 tmuxbot：bridge 短暂断连可以退避重试；CLI 未安装、
认证失败、pane 已不存在应 fail-fast；而“已写入 tmux 但没有看到回执”必须标记
`dispatch_uncertain`，先抓屏/查 transcript/由操作人确认，不能盲目重发 prompt（它可能已改过
代码或执行过命令）。

这也符合 AutoGen 对运行时职责的描述：运行时管理生命周期、通信、安全边界和监控，而不是
让业务代码直接管理每个 agent。见其
[Agent Runtime 文档](https://microsoft.github.io/autogen/stable/user-guide/core-user-guide/framework/agent-and-agent-runtime.html)。

### 3. “统筹负责计划，确定性内核负责执行”

[LangChain 的 supervisor/subagent 模式](https://docs.langchain.com/oss/python/langchain/multi-agent/subagents)
建议让中心 supervisor 选择专长 worker，并强调子 agent 的上下文隔离；它也明确指出简单场景
不应强行多 agent。映射到 tmuxbot：

- Coordinator CLI 输出一个版本化 `PlanProposal`（任务、角色、依赖、是否写入、验收条件、
  预算/超时），而非直接操纵其他 pane。
- `PlanValidator` 检查：唯一 task id、无环 DAG、角色能力、同项目单写规则、最大并发、
  reviewer 独立性、允许的工作目录。
- 风险动作（变更计划、扩大写权限、停止/重启会话、删除工作树）进入 `operator_required`；
  人在任一通道确认后以 Update 落库。
- 第一版保持 3–5 个显式角色，模板优先于任意自由群聊；只有并行读任务和不同 worktree 的写
  任务才允许并行。

这样既保留 Claude/Codex 等 CLI 的本地编码优势，也不会把 LLM 的自然语言输出当成数据库
状态机的授权来源。

### 4. 统一可观测性：RunEvent + trace + 指标，而非保存全部提示词

[OpenTelemetry Semantic Conventions](https://opentelemetry.io/docs/concepts/semantic-conventions/)
为 trace、metric、log、event、resource 提供一致命名；其
[GenAI 属性注册表](https://opentelemetry.io/docs/specs/semconv/registry/attributes/gen-ai/)
覆盖 provider、model、token、输入/输出与 tool 相关属性。AutoGen 也采用 OpenTelemetry 来
记录 agent runtime 的 trace，见其
[Tracing 文档](https://microsoft.github.io/autogen/stable/user-guide/agentchat-user-guide/tracing.html)。

建议在既有 SQLite `RunEvent` 之上增加“可选 OTel 导出”，不把遥测系统变成新的事实来源：

- Trace：`teamrun.create` → `task.dispatch` → `tmux.inject` → `provider.observe` →
  `artifact.register` → `review.decide`；使用 `teamrun_id` 作为 root trace correlation key，
  `task_id`、attempt、managed session、tmux target 作为属性。
- Metrics：ready/working/review 队列长度、dispatch 延迟、从注入到证据的耗时、重试数、
  租约等待/过期、provider/pane 可用率、人工介入率。不要把模型名、项目路径当高基数字段。
- Logs：保留 command digest、错误类别和证据 URI；提示词、附件路径、代码内容默认不导出。
  OTel 对 GenAI 可观测性也提醒内容采集涉及敏感数据；其官方说明指出默认应仅采元数据，
  内容采集需要明确 opt-in。
  [OpenTelemetry GenAI observability 说明](https://opentelemetry.io/blog/2026/genai-observability/)

异步任务依赖不要伪装成同步父子调用：跨 CLI 的依赖可用 trace link 关联；有持续时间的
`teamtask.dispatch`、`tmux.inject`、`cli.wait_for_output`、`teamtask.review`、`channel.reply`
使用 span，`ready`、`assigned`、`blocked`、`model.switched`、`dispatch.uncertain` 等瞬时
状态使用结构化 event。错误统一带 `error.type`。这与 OTel 的
[general event 约定](https://opentelemetry.io/docs/specs/semconv/general/events/)和
[trace 约定](https://opentelemetry.io/docs/specs/semconv/general/trace/)相符。

WebUI 的运行详情页应先做“事件时间线 + 当前租约 + 最近一次屏幕/JSONL 证据 + 操作历史”，
再考虑复杂的 agent 对话图。TG/飞书卡片只显示其摘要和深链。

## 分阶段建议

### Phase 1：可靠协作契约（优先）

1. 修复并测试任务图编辑：稳定 ID 不从标题派生；删除/改名时维护依赖。
2. 完善任务完成者身份：由真实 assignee/role 生成，而不是前端写死 Implementer。
3. 增加 `DispatchCommand` / outbox / receipt；为每次 tmux 注入分配 command id。
4. 对 write lease 加 heartbeat、到期和 pane 死亡处理；超时统一进入 `operator_required`。
5. 定义事件 schema 版本和 evidence level，所有通道只读投影。

验收：服务重启、WebSocket 断开、CLI 退出、重复点击“开始/完成/审查”都不会造成重复写入或
无证据完成。

### Phase 2：运行可见性与人工控制

1. TeamRun timeline、attempt 页面、outbox/receipt 检查页、按项目聚合的会话健康页。
2. OTel exporter 设为可选；先支持本地 JSON/SQLite 投影和 Prometheus 风格聚合，再决定是否接
   Jaeger/OTLP collector。
3. 通道操作统一成为 Update：暂停/恢复/停止、@ 策略、会话接管都返回相同的审计事件。
4. 引入 SLO：例如 dispatch p95、stuck task 数、需要人工介入的运行数；不以“模型回复了”
   作为完成指标。

建议在 Phase 1 结束时加入 checkpoint：以固定事件数量或一次 plan revision 为边界保存任务
快照、证据引用、tmux target、当前模型和上下文摘要，再归档旧事件。这样避免把完整终端文本
写入业务事件库，也为以后需要的长任务“继续新一轮”提供基础。Temporal 的事件历史概念及其
继续执行链路可作为参考：[Workflow Event History](https://docs.temporal.io/workflow-execution/event)。

### Phase 3：受限的自适应统筹

1. Coordinator 可提交 `PlanProposal` 与 replan，但只能经过 validator 产生新的 plan revision。
2. 支持 planner/implementer/reviewer/UI designer 等 AgentCard；每张卡独立声明 CLI、模型、
   工作目录、读写权限、上下文摘要策略和最大并发。
3. 并行写任务必须使用独立 git worktree 或显式共享写租约；汇合任务必须由 reviewer/合并角色
   处理冲突。
4. 为每一次模型切换、会话 `/new`、resume、context compaction 写 provenance event，避免
   运行图把“同一角色”误当作“同一上下文”。

## 现在不建议做的事

- 不要把 TeamRun 直接改成自由 agent 群聊；这会破坏单写、审查和可恢复性。
- 不要为了“持久化”立刻引入 Temporal/LangGraph/AutoGen 运行时。它们的持久性与运行时抽象
  值得借鉴，但当前单用户、单机、tmux-first 的需求可先由 SQLite outbox、lease 和 RunEvent
  达到；过早替换会使 tmux lifecycle、通道 binding 与 transcript 证据分裂。
- 不要默认采集完整提示、终端屏幕或附件到外部遥测后端；先按最小元数据、hash 与显式开关设计。

## 来源与采用边界

本文只采用官方文档或项目一手文档的模式，不复制其实现代码：

- [Temporal Workflow Execution](https://docs.temporal.io/workflow-execution)
- [Temporal Messages：Signals、Queries、Updates](https://docs.temporal.io/encyclopedia/workflow-message-passing)
- [Temporal Retry Policies](https://docs.temporal.io/encyclopedia/retry-policies)
- [Temporal Python 可观测性](https://docs.temporal.io/develop/python/platform/observability)
- [AutoGen Core：Agent Runtime](https://microsoft.github.io/autogen/stable/user-guide/core-user-guide/framework/agent-and-agent-runtime.html)
- [AutoGen Core：Message and Communication](https://microsoft.github.io/autogen/stable/user-guide/core-user-guide/framework/message-and-communication.html)
- [AutoGen：Tracing and Observability](https://microsoft.github.io/autogen/stable/user-guide/agentchat-user-guide/tracing.html)
- [LangChain：Subagents / Supervisor](https://docs.langchain.com/oss/python/langchain/multi-agent/subagents)
- [OpenTelemetry Semantic Conventions](https://opentelemetry.io/docs/concepts/semantic-conventions/)
- [OpenTelemetry GenAI Attributes](https://opentelemetry.io/docs/specs/semconv/registry/attributes/gen-ai/)
