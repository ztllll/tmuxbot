# 多 LLM 协调平台调研报告

日期：2026-07-11 UTC

状态：仅调研，不授权实现多 Agent 协调运行代码。

## 调研目标

研究 tmuxbot 如何在保留 tmux 核心的前提下，把当前共享项目目录的 Claude、Codex 独立会话逐步升级为可靠的多 LLM 协作平台。

## 已调研项目与可借鉴内容

### Claude Code Agent Teams

[官方文档](https://code.claude.com/docs/en/agent-teams)采用团队负责人、独立队员、共享任务列表和 Mailbox。队员可以直接通信，但官方明确说明 Agent Teams 不自动使用独立 worktree，同时修改相同文件容易产生冲突。

适合借鉴：任务状态、依赖、自主领取、队员消息、空闲通知。

### Codex Subagents

[官方文档](https://developers.openai.com/codex/subagents)采用主线程调度多个独立 Agent thread，可为探索、实现、测试、审查配置不同角色和模型。官方建议写操作较多时谨慎并行，并通过 worktree 隔离。

适合借鉴：主 Agent 聚焦决策、子 Agent 返回摘要、角色化配置、限制并发与递归深度。

### MetaGPT 与 ChatDev

[MetaGPT](https://arxiv.org/html/2308.00352)使用产品经理、架构师、项目经理、工程师和 QA 等角色，通过 SOP、结构化文档、消息池和订阅完成交接。[ChatDev](https://github.com/OpenBMB/ChatDev)进一步提供可配置工作流、可视化编排、中间产物和回放日志。

最重要的启发：Agent 之间应传递 PRD、设计、任务、代码、测试报告等结构化产物，而不是无限自然语言讨论。

### Magentic-One

[Magentic-One](https://www.microsoft.com/en-us/research/publication/magentic-one-a-generalist-multi-agent-system-for-solving-complex-tasks/)由 Orchestrator 维护任务账本和进度账本，判断是否完成、是否陷入循环、是否继续取得进展、下一个由谁执行；发生停滞后会重新规划。

适合借鉴：最大轮次、停滞阈值、重新规划、下一执行者选择、最终汇总。

### AutoGen、CrewAI、LangGraph、AgentScope

- [AutoGen](https://microsoft.github.io/autogen/stable/user-guide/core-user-guide/framework/agent-and-agent-runtime.html)：事件驱动 Runtime、Topic、Subscription、Handoff 和 Group Chat Manager。
- [CrewAI](https://docs.crewai.com/en/concepts/production-architecture)：Flow 管确定性流程和状态，Crew 管需要自主判断的复杂工作。
- [LangGraph Agent Server](https://langchain-ai.github.io/langgraph/tutorials/langgraph-platform/local-server/)：持久化 Run、任务队列、Checkpoint、Thread 串行化和流式事件。
- [AgentScope](https://github.com/agentscope-ai/agentscope)：统一事件系统、权限、Workspace、Sandbox、多会话服务和可观测性。

共同结论：确定性调度必须由程序控制，不能把所有流程判断都交给 LLM。

### OpenHands

[OpenHands Delegation](https://docs.openhands.dev/sdk/guides/agent-delegation)支持独立上下文、共享 Workspace、并行委派和统一汇总，整体形态与 tmuxbot 最接近。

适合借鉴：独立 Agent 上下文、Workspace 抽象、委派上限、失败结果归并。

### A2A Protocol

[A2A 1.0](https://a2a-protocol.org/v1.0.0/specification/)定义 Agent Card、Task、Message、Part、Artifact、流式状态更新和任务生命周期。任务完成、失败、取消或拒绝后不可重新启动；后续修改应创建同一 context 下的新任务。

适合借鉴：Agent 能力描述、任务 ID、上下文 ID、产物 ID、等待输入、认证等待、完成和失败状态。

## 实证研究的重要提醒

- [MultiAgentBench](https://arxiv.org/abs/2503.01935)显示，少量 Agent 可能改善协作，但继续增加数量可能因协调复杂度导致整体效果下降。
- [CAID](https://arxiv.org/pdf/2603.21489)表明，集中式任务分配、异步执行、隔离工作区、Git 分支合并和可执行测试，是软件工程多 Agent 更可靠的组合。
- [代码 Agent 框架实证研究](https://arxiv.org/html/2511.00872v1)指出，协调策略、反馈和推理深度比 Agent 数量更重要；多 Agent 的规划与反思阶段也会产生大量 token 开销。

## 对 tmuxbot 的建议原则

1. tmux pane 继续作为 Claude、Codex 的真实运行时，协调器只负责结构化派工和事件观测。
2. Python 程序负责任务状态、依赖、锁、超时、重试、审批和验收；LLM 负责规划、实现、测试和审查。
3. Agent 通信使用结构化消息，至少包含任务 ID、发送方、接收方、目标、限制、依赖、预期产物、状态、尝试次数和证据。
4. PRD、设计、diff、commit、测试报告和截图作为独立 Artifact 管理，避免反复复制进所有上下文。
5. 第一阶段只使用 Claude、Codex 两个 Agent，不追求 Agent 数量。
6. 共享目录模式禁止并发写入；并行写必须使用独立 Git worktree，最后排队合并和全量测试。
7. 必须检测停滞、重复消息、Git 状态不变、重复失败、产物缺失和 pane 长时间无活动。
8. Agent 自报完成不能作为验收依据，必须验证文件、commit、测试、lint 或部署结果。
9. 计划批准、危险操作、跨 Agent 合并、部署和冲突解决保留人工控制。
10. 所有派工、消息、状态、产物、审批、成本和结果写入追加式事件日志。

## 建议的未来立项阶段

以下阶段目前均不实施：

1. 定义 AgentCard、Task、Message、Artifact、TeamRun 和事件日志协议。
2. 做 Claude + Codex 顺序协作试点，共享目录但使用独占写锁和明确角色交接。
3. 建立固定任务评测集，对比单 Agent 与双 Agent 的成功率、耗时、token、人工干预和回归率。
4. 引入 Git worktree、任务所有权、commit 产物、合并队列、冲突检测和测试门禁。
5. 增加能力路由、依赖并行、停滞检测、重新规划和有限重试。
6. 最后通过 `/panel` 展示团队、任务、Agent、产物、审批、成本和停止控制。

## 暂缓决策

在任务协议和评测体系建立前，不决定直接依赖 AutoGen、CrewAI、LangGraph、A2A 或其他大型框架。它们的设计模式值得借鉴，但 tmuxbot 的核心价值是操作用户已有 tmux 中的真实 CLI；过早采用 API 型 Agent Runtime 可能反而破坏这一优势。
