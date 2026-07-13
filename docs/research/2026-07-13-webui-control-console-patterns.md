# WebUI 控制台交互模式调研

日期：2026-07-13 UTC
范围：tmux 窗口观察/操作、项目管理、创建向导、多 Agent 角色与调度、明暗主题。只参考官方文档或官方仓库。

## 结论

这四项都适合做成一个统一的「项目控制台」，而不是给现有页面分别加入口：项目是持久对象；每次运行是项目下的会话/TeamRun；tmux pane 是会话的实时执行端；Telegram、飞书和 Web 读取同一运行状态。终端默认只能观察，用户显式接管后才能写入；创建流程按“路径 → 可用 CLI → 角色与调度 → 确认”逐步收集信息。

## 可借鉴模式

| 需求 | 官方参考与事实 | 建议采用的产品模式 |
| --- | --- | --- |
| tmux 窗口直接查看/操作 | [Coder Web Terminal](https://coder.com/docs/user-guides/workspace-access/web-terminal) 使用 xterm.js + WebSocket，并保持可重连的持久终端；[ttyd](https://github.com/tsl0922/ttyd) 明确支持只读默认、以 `--writable` 开启写入，以及将 tmux 会话作为共享目标；[WeTTY](https://github.com/butlerx/wetty) 也以 xterm.js/WebSocket 提供浏览器终端。 | 项目页内嵌「终端」抽屉/全屏页：先 attach 到既有 tmux session/pane，默认 Observe；点击“接管输入”才获得临时写权限，退出、断线或超时即恢复 Observe。把终端字节 WebSocket 与项目/运行状态事件分开，接管开始/结束记审计事件。不要把未鉴权的完整 shell 暴露到 Web。 |
| 已有项目管理 | [OpenHands Agent Canvas](https://github.com/OpenHands/agent-canvas) 支持在 UI 添加多个 backend 并切换；其 [workspace mode 变更](https://github.com/OpenHands/agent-canvas/pull/1293) 将已选本地目录默认设为直接工作目录，同时把“新 worktree”作为显式选项；[Coder workspace management](https://coder.com/docs/user-guides/workspace-management) 将 workspace 作为独立的可管理资源。 | 首页应是 Projects，不是 tmux sessions：展示路径、Git、最近运行、活跃 CLI、健康状态；提供新建、导入/发现、编辑名称/路径/默认配置、归档/删除（删除前先停止受管运行）。会话仅是项目的子资源，支持恢复、归档和从未托管 tmux session 认领。 |
| 创建项目与 CLI 选择 | OpenHands 的 [agent 自动发现提案](https://github.com/OpenHands/agent-canvas/issues/405) 指出：可用 CLI 应由运行环境探测，不应让用户猜测配置；现有对话也应按 agent 类型标识并可恢复。 | 第一步只输入/选择项目路径并校验读写、Git、现有 tmux/CLI session；第二步仅显示已探测且可启动的 CLI（Claude Code、Codex 等），预填推荐项；把高级 flags 收进“高级设置”，不要放在首次路径输入页。 |
| 角色、模型与调度方式 | [CrewAI 的 process 文档](https://docs.crewai.com/en/concepts/processes) 区分默认的顺序执行与需要 `manager_llm`/manager agent 的层级调度；其 [hierarchical guide](https://docs.crewai.com/en/learn/hierarchical-process) 明确 manager 负责委派和验收。 [LangChain 的 supervisor/subagent 文档](https://docs.langchain.com/oss/python/langchain/multi-agent/subagents) 将监督者与专业 worker 分开，监督者在多轮中动态选择 worker。 | 第三步使用可编辑模板而非空白编排图：`单 CLI`、`顺序交接`（规划→实现→审查）、`主管调度`（Coordinator→按需委派）、`并行实现+汇总审查`。每一个角色卡都必须独立设置 **CLI、模型、职责、工作目录/隔离方式、可否委派、开始条件与输出去向**；主管模型与执行模型不要被绑定为同一个字段。第一版固定 3–5 个角色，先不开放任意 DAG。 |
| 暗黑界面 | Coder 的官方实现已将 `theme_mode`、亮/暗主题槽位和终端字体放进用户外观设置（[后端提交](https://github.com/coder/coder/commit/024132e8a49aba761cc99cbbf1c8148e8157150f)）；其前端包含 system preference、模式解析、主题选择器和预览组件（[前端提交](https://github.com/coder/coder/commit/e8cfff40b4d1a3aaf749e3f28b7f7358ad015deb)）。 | 顶栏提供“跟随系统 / 深色 / 浅色”三档，持久化为用户偏好；首屏在服务端或内嵌初始化数据中带入偏好，避免先闪浅色再变深色。终端色板独立设置，但与应用主题同步给出可读的默认值。 |

## 推荐的信息架构与向导

```text
Projects
  └─ Project detail
       ├─ Overview / Runs / Terminal / Files & Git / Settings
       └─ New run
            1. Project path        discover + validate
            2. Runtime CLIs        probe, choose, resume existing session if present
            3. Team recipe         choose template; assign CLI + model per role
            4. Review & launch     show panes, permissions, isolation and handoffs
```

角色模板的默认值应尽量保守：

- **快速实现**：执行者一个角色，适用于小改动。
- **标准工程**：规划者 → 实现者 → 审查者；审查必须看到 diff/测试结果，而非只接收实现者的文字结论。
- **UI 功能**：规划者 → UI 设计者 → 实现者 → 审查者；设计者输出结构化验收点/视觉说明，执行者才开始改代码。
- **主管调度**：Coordinator 选择或复用工作者；仅在任务不确定、可分解时启用，且要求单独选择 coordinator 的 CLI 和模型。

## 不建议照搬

- 不把 ttyd/WeTTY 当作完整控制面：它们解决的是浏览器 TTY，不管理项目、受管会话、接管审计或跨渠道状态。
- 不让 Web 页面直接执行任意 tmux/shell 命令；目标只能由服务器从已登记的项目/会话解析，写入需短期接管授权。
- 不让用户先画 DAG 再能启动一次普通任务。先用模板和可编辑角色卡验证领域模型；当运行记录证明有稳定复用需求时，再增加图形编排。
- 不将“模型”只设为项目全局默认。调度、实现、审查和设计往往应能各自选择 CLI/模型，并在运行快照中冻结，保证可追溯与可恢复。
## 对本项目的落地优先级

1. 将已有 Web 终端能力接到项目详情页，并完成 Observe/Takeover 的显性 UI 与审计可见性。
2. 引入 Project CRUD、tmux/CLI discovery 与 session adopt；不要让项目管理依赖手工编辑配置文件。
3. 加入三档主题与启动时无闪烁的偏好恢复。
4. 再实现四步向导和上述四个团队模板；先持久化“角色定义 + 运行快照”，随后才接真实多 CLI 调度。
