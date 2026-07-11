# WebUI 多 Agent 开源项目调研

日期：2026-07-11 UTC

状态：仅调研，不授权复制第三方代码或开始 WebUI 实施。

## 调研结论

tmuxbot 的方案 B 可行，而且公开项目已经分别验证了关键组成部分：PTY+xterm.js 的 tmux Web 终端、CLI 自动探测、Hooks/Transcript 状态识别、MCP 消息、Git worktree、任务 DAG、文件声明、Artifact、移动端控制和故障恢复。

没有一个候选项目同时覆盖 tmuxbot 已有的 Telegram、飞书、附件、原生 provider session resume、Runtime V2 和统一富消息。因此不建议替换 tmuxbot，也不建议直接嵌入某个完整项目；应吸收明确模式并在现有核心上实现。

## 候选项目概览

以下活跃度和 Star 数为本次检索时快照，后续会变化。

| 项目 | 主要能力 | 许可/风险 | 建议 |
|---|---|---|---|
| [Parallel Code](https://github.com/johannesjo/parallel-code) | 多 CLI、自动 worktree、diff 审查、移动访问、服务端口 | MIT，约 832 Star | 可参考 worktree 生命周期和任务级终端 |
| [Agent Deck](https://github.com/asheshgoplani/agent-deck) | Session 管理、Conductor、MCP、fork/resume、worktree、通知 | MIT，约 476 Star | 高价值参考，但不要整体引入 |
| [agent-dashboard](https://github.com/bjornjee/agent-dashboard) | Hooks、Transcript、状态分组、PWA、审查门禁 | MIT，约 17 Star | 适合 Provider 状态与移动端设计 |
| [Guppi](https://github.com/ekristen/guppi) | PTY+xterm.js、tmux control mode、状态通知 | MIT，约 11 Star | 适合 Web 终端和 session discovery |
| [Session Deck](https://github.com/JesseProjects-LLC/session-deck) | 多终端布局、SQLite、认证、PWA | MIT，约 8 Star | 适合布局、认证和持久化参考 |
| [ruah-orch](https://github.com/ruah-dev/ruah-orch) | DAG、worktree、文件 claims、Artifact、takeover/resume | MIT，约 11 Star | 适合调度和并行写入协议 |
| [TermHive](https://github.com/0x0funky/TermHive) | Grid/Canvas、多 Agent WebUI、Wiki、MCP 消息 | 未发现许可证 | 只能借鉴产品思想，不能复制代码 |
| [webmux](https://github.com/windmill-labs/webmux) | worktree、终端、PR/CI、端口、Docker、移动 Chat | 未发现许可证 | 只能借鉴产品思想，不能复制代码 |
| [Claude Squad](https://github.com/smtg-ai/claude-squad) | tmux + worktree 多 Agent 管理 | AGPL-3.0，约 8084 Star | 只参考架构思想，不复制代码 |
| [Claude Codex Bridge](https://github.com/SeemSeam/claude_codex_bridge) | 多 Provider、移动端、角色、共享记忆、跨 Agent ask | AGPL-3.0，约 3238 Star | 只参考产品能力，不复制代码 |
| [NTM](https://github.com/Dicklesworthstone/ntm) | tmux swarm、任务图、mail、locks、审计、API | 自定义许可含 OpenAI/Anthropic 排除条款 | 排除代码复用和实现分析 |

## 最值得吸收的设计

### 1. PTY 只负责浏览器连接，tmux 继续负责终端真相

Guppi 的结构非常接近我们的需求：每个浏览器终端创建一个 PTY，PTY 执行 `tmux attach-session`，xterm.js 通过 WebSocket 收发字节；tmux 自己负责渲染、多客户端同步和会话存活。

建议吸收：

- 浏览器断开只销毁 PTY client，不影响 tmux。
- Session/window/pane 变化通过 tmux control mode 或独立 discovery stream 推送。
- Terminal WebSocket 与业务事件 WebSocket 分离。
- 使用 tmux 原始渲染，不自己模拟 TUI 屏幕。
- 明确鼠标模式、浏览器文本选择和剪贴板快捷键冲突。

### 2. 移动端不要压缩桌面终端墙

webmux 和 agent-dashboard 都为手机提供简化界面，而不是把完整桌面控制台缩小。

建议吸收：

- 桌面端显示 Run Spine、Agent 列表、任务和终端坞。
- 手机端默认显示 Agent 状态、简短对话、审批和单个终端。
- 手机端一次只打开一个 Agent，避免四终端宫格无法操作。
- 浏览器通知只针对 waiting、error、approval 和 completed 等状态变化。

### 3. Provider 状态必须由 Adapter、Hook 和 Transcript 共同判断

agent-dashboard 使用 Provider Adapter、Hooks 和 JSONL Transcript 识别 blocked、waiting、running、review、PR 和 merged。Guppi 也通过 Hook 增强 `pane_current_command` 的粗粒度状态。

建议吸收：

- 保留 tmuxbot 当前 terminal status + transcript + hooks 多信号融合。
- ProviderProfile 声明支持哪些信号，缺少信号时明确降级。
- WebUI 显示状态证据来源，避免将屏幕正则结果伪装成确定事实。
- Hook 安装必须可检查、可撤销，并且不能覆盖用户已有配置。

### 4. MCP 配置应该 session-scoped，禁止污染全局配置

TermHive 为每个 Agent 建立 session-scoped MCP 配置；Agent Deck 还提供 MCP socket pool，减少大量 Agent 重复启动 MCP 服务的资源开销。

建议吸收：

- 每个 AgentInstance 生成独立 Coordinator MCP 配置。
- Claude 优先使用 session 参数或项目级配置，不覆盖 `~/.claude.json`。
- Codex 配置项必须有稳定命名空间和可回滚记录。
- Coordinator MCP 服务可以按项目共享，但鉴权 token 和 Agent 身份必须独立。
- 后期可增加 Unix socket pool，V1 不必实现。

### 5. Agent 消息必须经过中心邮箱，不能直接无记录注入

TermHive 的 MCP 消息最终作为新输入写入目标 PTY，证明跨 CLI 消息在工程上可行。但直接注入容易被误认为用户指令，也容易形成循环。

tmuxbot 应保留更严格的方案：

- `send_message` 先写入数据库并生成稳定 message ID。
- 消息包含发送方、接收方、run/task、目标、Artifact 和 attempt。
- 调度器去重、检查权限和循环阈值后才投递。
- 投递到 TUI 时使用明显的系统信封格式，不能伪装成 Boss。
- Agent 回复仍写回中心邮箱，不建立无监管 P2P 通道。

### 6. Worktree 需要完整生命周期，不只是 `git worktree add`

Parallel Code、Agent Deck、Claude Squad 和 webmux 都验证了每任务 worktree。Agent Deck 的 `.worktreeinclude` 和 setup/destruction hook 尤其值得采用。

建议吸收：

- 每个并行写任务拥有 branch、worktree、Agent、端口和 Artifact。
- 支持 `.worktreeinclude`，只复制显式允许且 gitignored 的必要文件。
- 禁止默认复制全部 `.env`、`node_modules`、数据集和虚拟环境。
- 支持项目级 post-create setup 和 pre-remove teardown hook。
- Hook 必须有超时、日志和失败状态，不能静默失败。
- 依赖目录优先软链接或运行 setup，不盲目深拷贝。
- 关闭任务前检查未提交修改、运行进程、容器和端口。

### 7. 并行写入必须结合 Claims、Artifact 和 DAG

ruah-orch 的重要启发是：branch 和 worktree 只隔离 checkout，不能解决逻辑冲突。它在任务启动前检查文件 glob claims，并让依赖任务按 DAG 等待。

建议吸收：

- Task 可声明预期修改范围和只读范围。
- 重叠写 claims 在派工前阻止，而不是合并时才发现。
- 新文件无法完全预测，因此 claims 只是前置门禁，不是绝对保证。
- Task 完成必须保存实际修改文件、commit、测试和兼容性信号。
- Parent task 在 child task 合并或取消前不能进入集成阶段。
- 失败任务支持 takeover，保留原 worktree、Artifact 和失败证据。

### 8. Session 生命周期必须支持 adopt、archive、fork 和 orphan recovery

Agent Deck 对 session fork/resume/archive 和 orphan worktree cleanup 做得较完整，agent-dashboard 也强调 tmux session 与控制面解耦。

建议吸收：

- Web 服务重启后 reconcile，而不是重建所有 CLI。
- 未登记但存在的 tmux 会话显示为 orphan，可选择 adopt、ignore 或 archive。
- Archive 停止 Agent 但保留 transcript、metadata 和 worktree。
- Fork 必须优先调用 Provider 原生 fork；不支持时才采用新会话 + checkpoint。
- 删除 worktree 和删除 Provider conversation 是两个独立动作。

### 9. 项目长期知识和运行期共享内容需要分开

TermHive 区分 Project Wiki 和 Shared Content；CCB 也使用项目共享记忆文件。这验证了“长期知识”和“本次运行产物”不应混在一个 growing transcript 中。

建议吸收：

- 长期规则继续由 `CLAUDE.md`、`AGENTS.md` 和正式 specs 管理。
- TeamRun 共享内容进入 Artifact store，不自动写回项目宪法。
- 只有经过人工确认的稳定决策才能进入长期文档。
- 不允许多个 Agent 同时更新同一个 wiki 或 decision 文件。
- Agent 启动时注入索引和引用，不把整个知识库复制进 prompt。

### 10. 任务状态、审查和通知必须来自事件转换，而不是轮询猜测

agent-dashboard 的状态分组和 Agent Deck 的 transition notifier 表明，UI 和通知最好消费统一的状态转换事件。

建议吸收：

- `RunEvent` 是 WebUI、TG、飞书和通知的共同输入。
- 只有状态真正发生转换时发送通知。
- 每个状态转换记录原因、证据来源和触发者。
- UI 的 Run Spine 直接从 Task/RunEvent 投影，不维护第二份状态。
- Hook、Transcript 和终端状态只产生 ProviderEvent，再由 reducer 生成稳定状态。

## UI 可借鉴点

### 可以采用

- TermHive 的递归 Grid split、可拖动 divider 和 Agent 固定颜色身份。
- Guppi 的等待/错误 alert banner 与后台 Web Push。
- Session Deck 的 workspace 持久化、拖动布局和 PWA。
- agent-dashboard 的状态分组、diff viewer、计划/图表 viewer 和移动审批。
- Parallel Code 的 task focus mode、任务 notes 和 PR/CI 状态。
- webmux 的桌面控制台与移动简化 Chat 分离。

### V1 暂缓

- 自由 Canvas 八方向缩放：实现和移动适配成本较高。
- 多主机 SSH 聚合：与单机单用户定位不符。
- Agent race/arena：容易放大 token 消耗，先建立评测后再做。
- 自动调用 Provider API 生成分支名：违背 V1 不引入 API worker 的边界。
- 完整 PR 管理：先做好 diff、commit 和测试 Artifact。

## 不应照搬的设计

1. 无认证默认启动 Web 终端。Web 终端等同 Shell，tmuxbot 必须首次启动就要求认证。
2. 让 Agent 直接互发未记录的终端输入。必须经过 mailbox、去重和权限检查。
3. 自动覆盖 `CLAUDE.md`、`AGENTS.md` 或全局 MCP 配置。应使用 managed fragment、独立配置或可撤销 patch。
4. 所有 Agent 共享一个可写 Wiki。会产生竞争和不可信长期记忆。
5. 仅凭 Agent 自报 done 自动合并。必须检查 Artifact、Git 和测试证据。
6. 默认复制所有 gitignored 文件到 worktree。可能复制密钥和巨大目录。
7. 用一个 Conductor LLM 保存全部任务真相。调度状态必须持久化在程序中。
8. 为了并行而广播所有上下文。应按任务路由最小上下文。

## 对现有设计规格的建议修订

现有 `2026-07-11-webui-multi-agent-control-plane-design.md` 的总体架构不需要推翻。建议在正式实施计划前补充以下明确要求：

1. Session-scoped Coordinator MCP 配置和全局配置保护。
2. Worktree `.worktreeinclude`、setup/teardown hooks、端口和进程清理。
3. File claims 只是前置冲突门禁，完成后仍以实际 diff 验证。
4. Orphan session 的 adopt/archive/ignore 流程。
5. 桌面 Command Center 与移动简化视图分离。
6. Terminal observe/control 模式和接管审计。
7. RunEvent 作为 WebUI、TG、飞书和通知的唯一状态输入。
8. 长期知识、TeamRun Artifact 和 Provider 私有 transcript 三层分离。

## 最终推荐

直接代码参考优先级：

1. Guppi：PTY、xterm.js、tmux control mode。
2. agent-dashboard：Provider Adapter、Hook/Transcript、状态投影和 PWA。
3. Parallel Code：任务级 worktree、diff、移动访问。
4. ruah-orch：DAG、claims、Artifact、takeover/resume。
5. Agent Deck：fork/resume/archive、`.worktreeinclude`、生命周期 hook 和通知。
6. Session Deck：认证、SQLite 和终端布局。

TermHive、webmux 因缺少明确许可证，只能借鉴公开产品思想。Claude Squad 和 CCB 为 AGPL，只能参考架构概念。NTM 包含针对 OpenAI/Anthropic 的自定义排除条款，不进入代码复用或实现分析范围。

