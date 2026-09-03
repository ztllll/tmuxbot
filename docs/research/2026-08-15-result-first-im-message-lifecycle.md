# Telegram / 飞书结果优先消息生命周期调研

日期：2026-08-15

## 结论

老板提出的方向正确，而且应成为 tmuxbot 的正式通道产品原则：

> **IM 是结果与人工决策面，不是终端日志镜像。**
>
> 一个模型 turn 最多维护一条低打扰的“过程投影”，持续原地更新；只有可消费的文字、重要结论、需要人工输入、阻塞/失败和产物附件才产生独立消息。

项目已经有实现基础：`ProviderEvent`、tool aggregator、Codex plan 单卡更新、Telegram message edit、飞书 Card JSON 2.0 / CardKit streaming、统一 `ReplyDocument`。不需要推倒重来；真正缺少的是统一的内容分级、消息生命周期和通知政策。目前的 `assistant_tools`、`assistant_text_delta`、`assistant_live_text`、`assistant_text` 分支分别演进，导致“过程卡”和“最终答复”的边界不稳定，部分 provider 的文字 delta 甚至直接占据最终消息。

推荐引入一个通道无关的 **TurnProjection** 深模块：它消费标准化 provider events，持有每个 route/turn 的过程卡与最终答复状态，再投影到 Telegram 和飞书。Provider adapter 只报告事实，不再决定发几条消息；Frontend 只负责 create/update/finalize，不再判断语义。

## 既有调研审计

历史 Git 中已有以下设计，可继续复用：

| 历史成果 | 可复用结论 | 当前不足 |
| --- | --- | --- |
| `57f9794` 跨通道富消息设计 | `ReplyEnvelope -> ReplyDocument -> Telegram/Feishu renderer`；通道输出互不解析 | 重点是最终回复结构，没有完整定义过程压缩政策 |
| `5278ba7` 无按钮状态卡设计 | 过程/结果使用统一状态色；操作统一走 slash command | 没有定义一个 turn 最多几条消息 |
| `1de72e4` Telegram 状态标识 | TG 用 Emoji 状态行模拟飞书彩色 header | 只解决视觉等价，不解决信息分级 |
| `2b3a64a` Telegram 消息润色 | 状态/计划/tool 与 final reply 分离 | 对 text delta / live text 的生命周期仍偏 provider-specific |
| `20258d1` 通道控制面板 | IM 是轻量控制面，tmux 是执行面 | 未落成统一的 turn 投影模型 |
| `docs/research/2026-07-17-tmux-multi-cli-next-steps.md` | TG/飞书只显示事件摘要和深链；完整执行证据留在 Web/tmux | 面向 TeamRun，可扩展到普通单 CLI turn |

现有代码能力：

- `ProviderEventKind`: `TEXT_DELTA`、`FINAL_TEXT`、`TOOL_PROGRESS`、`PLAN_UPDATE`、`INTERACTION_REQUEST`、`LIFECYCLE_CHANGE`、`PROVIDER_ERROR`；
- `assistant_tools` 已聚合到同一条 status message；
- Codex `update_plan` 已独立维护一条可编辑计划消息；
- Telegram 支持 `editMessageText` 路径；
- 飞书支持 Card JSON 2.0 全卡 PATCH，且已有可选 CardKit streaming；
- 最终答复已有统一 `ReplyDocument`、分页、附件与 provider footer。

因此现状不是“没有能力”，而是能力散落在 `jsonl.py` 多个分支中，缺少一个统一 owner。

## 官方平台能力

### Telegram

Telegram Bot API 官方文档：

- [`editMessageText`](https://core.telegram.org/bots/api#editmessagetext) 可编辑机器人发送的普通文本消息；成功返回更新后的 `Message`，正文最多 4096 characters after entity parsing。
- [`sendChatAction`](https://core.telegram.org/bots/api#sendchataction) 明确用于替代“正在处理，请等待”这类文本消息；typing 状态适合短等待，不应产生聊天历史。
- [`deleteMessage`](https://core.telegram.org/bots/api#deletemessage) 一般只允许删除 48 小时内的消息；过程卡不应依赖事后清理来维持整洁。
- 当前 Bot API 已提供 `sendRichMessageDraft` 和可编辑 rich message，但这是较新的能力；首期应继续基于成熟的 `sendMessage + editMessageText`，将 rich draft 作为后续增强，避免 aiogram/客户端兼容风险。

产品含义：TG 没有原生卡片背景或独立 card entity，但一条 HTML 消息可以充当“过程卡”，使用 message ID 原地编辑。更新频率应由应用主动节流，避免 API flood control 和无意义闪烁。

### 飞书

飞书开放平台官方文档：

- [更新应用发送的消息卡片](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/im-v1/message/patch)：按 `message_id` PATCH；更新前后都要求 `update_multi=true`；卡片不超过 30KB；普通 PATCH 限 1000/min、50/s；发送后 14 天内可更新。
- [Card JSON 2.0](https://open.feishu.cn/document/feishu-cards/card-json-v2-structure)：支持 `summary`、共享更新和 `streaming_mode`；JSON 2.0 需 Feishu 7.20+。
- [CardKit 流式更新](https://open.feishu.cn/document/cardkit-v1/streaming-updates-openapi-overview)：可全卡、局部组件或文本流式更新；单 card entity 操作上限 10/s；旧文本是新文本前缀时可继续打字机输出；10 分钟无活动自动关闭，但官方建议主动关闭。
- [流式更新文本](https://open.feishu.cn/document/cardkit-v1/card-element/content)：要求严格递增 `sequence`、`update_multi=true`，单文本内容最长 100000 characters。
- 流式模式中要处理交互 callback，必须先关闭 streaming mode。

产品含义：飞书天然适合作为可更新过程卡；普通 Card PATCH 已足够实现阶段摘要，CardKit streaming 适合最终答案文字流，但不应该把每一条工具日志原样做打字机输出。

## 建议的领域语言

### Turn

一次用户输入到一个 provider 最终答复之间的完整工作周期。一个 route 同时最多有一个活跃 Turn；steering 消息属于当前 Turn，follow-up 创建下一个 Turn。

### ProgressProjection（过程投影）

Turn 的低打扰、可覆盖状态。它是执行事实的摘要，不是日志。每个 Turn 最多一个 ProgressProjection message/card。

建议字段：

- state：queued / working / waiting / blocked / failed / completed；
- phase：理解、检索、修改、测试、审查、收尾等稳定阶段；
- current_action：一行当前动作；
- completed_steps：最多 3 条最近关键进展；
- counters：文件数、测试数、工具数、elapsed；
- plan_summary：计划版本与完成度；
- warning：至多一个当前风险；
- updated_at。

禁止放入：完整 shell 输出、逐文件 diff、thinking、重复工具参数、长测试日志、模型自言自语。

### ResultMessage（结果消息）

用户真正需要阅读、可以独立消费的最终文字。每个 Turn 正常情况下只产生一个 ResultMessage，使用统一结构：

1. 直接结论；
2. 关键变更/发现；
3. 验证证据；
4. 风险或遗留；
5. 附件/产物；
6. provider footer。

ResultMessage 独立发送以触发 IM 通知；不覆盖 ProgressProjection，因为用户可能需要回看简短过程摘要。

### AttentionMessage（注意消息）

需要用户现在做决定或发生非正常终态时独立发送：

- waiting：需要补充信息、选择或外部授权；
- blocked / failed：任务无法继续或验证失败；
- security / destructive confirmation：高风险确认；
- delivery uncertainty：消息/命令是否实际执行不确定。

普通“正在读取文件”“正在跑测试”不属于 AttentionMessage。

### ArtifactMessage（产物消息）

只有用户可消费的图片、报告、补丁、归档等才单独上传。中间临时文件不发送。

## 推荐消息政策

### 默认 Turn 的消息预算

| 消息类型 | 每 Turn 默认预算 | 是否通知 | 更新方式 |
| --- | ---: | --- | --- |
| ProgressProjection | 0 或 1 条 | 首次可静默/低打扰 | 原地 edit / PATCH |
| ResultMessage | 1 条 | 是 | 单独发送；长内容分页/附件 |
| AttentionMessage | 按需，通常 0–1 条 | 是 | 单独发送 |
| ArtifactMessage | 按最终产物数量 | 是 | 原生附件 |
| PlanProjection | 并入 ProgressProjection；只有完整计划需用户确认时独立 | 否/按需 | 原地更新 |

目标不是机械限制消息数，而是把普通成功 Turn 收敛为 **1 条过程卡 + 1 条结果卡**；短任务甚至只有 1 条结果卡。

### 过程卡显示规则

创建条件：

- 预计耗时超过 3–5 秒；或
- 第一个 TOOL_PROGRESS / PLAN_UPDATE 到达；或
- provider 明确进入 working。

更新触发：

- phase 改变；
- 当前动作的语义摘要改变；
- plan 完成度改变；
- waiting/blocked/error；
- 至少经过节流窗口。

不因以下事件更新：

- token/usage 的每次变化；
- thinking chunk；
- 同一工具的重复 stdout；
- 文件逐行修改；
- 仅时间流逝（除非显示长任务 elapsed 且间隔较大）。

建议节流：

- Telegram：最快 1.5–2 秒一次，且内容 hash 变化才 edit；
- 飞书普通 PATCH：最快 1 秒一次足够，远低于官方 50/s；
- 飞书 CardKit：平台虽允许单卡 10/s，但过程摘要建议仍为 1–2 秒一次；最终文字打字机可更快。

终态：

- success：过程卡改为绿色/✅，压缩成 2–5 行摘要，不附完整代码过程；
- waiting：橙色，显示用户需要提供什么；
- failed/blocked：红色，保留最后成功阶段与错误摘要；
- 如果 Turn 在阈值内直接完成且没有重要过程，不创建过程卡。

### 内容分类建议

| Provider event | 默认投影 |
| --- | --- |
| TOOL_PROGRESS | 归并到 ProgressProjection；同类工具折叠计数 |
| PLAN_UPDATE | 更新过程卡内计划摘要；仅当需要用户批准完整计划时独立 AttentionMessage |
| TEXT_DELTA | 不直接占聊天历史；缓存为 ResultDraft。飞书可选流式写最终卡，TG 首期只保留 typing/过程卡 |
| FINAL_TEXT | 生成 ResultMessage；关闭 ResultDraft |
| INTERACTION_REQUEST | 独立 AttentionMessage，同时过程卡置 waiting |
| LIFECYCLE_CHANGE | compaction/wake 等归并到过程卡；只有失败或需操作时独立通知 |
| PROVIDER_ERROR | 可恢复时只更新过程卡；确认终态失败后发 AttentionMessage |
| USAGE_UPDATE | 只进入最终 footer/状态命令，不触发消息 |

## 推荐状态机

```text
Turn opened
  ├─ quick final --------------------------> ResultMessage only
  └─ meaningful work
       -> ProgressProjection(working)
       -> update phase/summary in place
       ├─ needs user -> Progress(waiting) + AttentionMessage
       ├─ failed     -> Progress(failed)  + AttentionMessage
       └─ final      -> Progress(completed, compact)
                     + ResultMessage
                     + optional ArtifactMessage(s)
```

关键不变量：

1. 每个 route/turn 只有一个活跃 ProgressProjection；
2. FINAL_TEXT 至多生成一个 ResultMessage；
3. 可恢复 provider error 不单独刷屏；
4. 过程卡失败不阻止最终结果发送；结果发送失败不能推进 transcript offset；
5. 更新必须基于 stable turn id + message id，并带内容 hash / sequence；
6. bridge 重启后可恢复或安全放弃旧过程卡，但不能重发已确认的最终结果。

## 模块设计

建议新增深模块：

```text
ProviderEvent
   -> TurnProjection.reduce(event)
        -> ChannelIntent[]
             progress.create
             progress.update
             progress.finalize
             result.publish
             attention.publish
             artifact.publish
   -> TelegramProjectionAdapter / FeishuProjectionAdapter
```

### TurnProjection

这是语义 owner：决定信息级别、消息预算、状态迁移、摘要、去重和何时通知。它不调用 Telegram/飞书 API。

最小接口可保持为：

```python
intents = projector.consume(binding, provider_event, now)
```

内部持久/内存状态：turn_id、progress message reference、result draft、phase、recent summaries、last render hash、last update time、terminal state。

### Channel adapter

只负责平台机制：

- Telegram：create/edit/finalize HTML message；处理 4096 与 flood retry；
- Feishu：create/PATCH Card V2；可选 CardKit entity + component streaming；维护 sequence；
- 两端统一返回 message receipt，供 offset 和恢复逻辑使用。

### 摘要来源

首期不要再调用额外 LLM 总结过程。优先使用确定性映射：

- tool name + safe parameters -> “正在读取 4 个文件”；
- edit/write -> “已修改 3 个文件”；
- test command/result -> “测试 42 项通过”；
- plan status -> “计划 2/4”；
- lifecycle -> “正在压缩上下文”。

无法识别的 tool progress 只显示最近一条截断摘要，不回吐原始 payload。

## Telegram 与飞书投影

### Telegram

首期推荐：

- `sendChatAction(typing)` 维持短等待反馈；
- 超过阈值创建一条 `🟡 工作中` HTML 消息；
- `editMessageText` 原地刷新；
- 完成后将该消息压缩为 `✅ 过程摘要`；
- 最终结果使用独立富消息发送并触发通知；
- 不对 text delta 做逐 token edit，避免 flood、闪烁和最终卡混淆。

后续可评估 `sendRichMessageDraft`，但只用于 ResultDraft，不用于工具日志。

### 飞书

首期推荐：

- ProgressProjection 使用 Card JSON 2.0 + 普通 whole-card PATCH，功能最稳；
- `update_multi=true`，header 颜色表达状态，summary 用于会话列表预览；
- 最终 ResultMessage 使用独立绿色/信息卡；
- CardKit streaming 仅用于长最终文字的 ResultDraft，可灰度开启；
- 结束时主动关闭 streaming mode，再允许 callback/forward；
- 过程摘要仍按 1–2 秒节流，不使用平台 10/s 上限疯狂刷新。

## 分阶段实施路线

### Phase 0：基线与观测

- 记录每 Turn 当前产生的消息数、edit 次数、最终消息数、重复 final 数和平均正文长度；
- 增加不含内容的 debug/audit counters；
- 固定三类回放 fixture：短问答、代码修改+测试、等待用户输入。

验收：能量化改造前后的“消息噪声”。

### Phase 1：统一过程卡（最高优先）

- 新增 `TurnProjection` 与 `ChannelIntent`；
- 将 `assistant_tools`、`assistant_plan`、可恢复 lifecycle/error 合并为每 Turn 一条过程卡；
- tool 输出转换为确定性摘要与计数；
- TG/飞书统一 create/update/finalize receipt；
- 过程卡节流、hash 去重、成功后压缩。

验收：典型代码修改 Turn 在 TG/飞书都不超过 2 条文本消息（过程+结果），多次 tool/plan 更新只编辑同一 message ID。

### Phase 2：结果草稿与最终消息分离

- `TEXT_DELTA` 只进入 `ResultDraft`，不再直接创建普通消息；
- TG 首期不展示逐 token draft，只保留 typing + 过程卡；
- 飞书可选 CardKit 流式 ResultDraft；
- `FINAL_TEXT` 原子发布独立 ResultMessage，并关闭/替换草稿；
- 解决当前流式 finalize 绕过统一 `ReplyDocument` 的技术债。

验收：无论 provider 是否产生 delta/live/final，最终只有一个结构一致的 ResultMessage，footer、附件、分页都一致。

### Phase 3：注意消息和可恢复性

- interaction / waiting / blocked / terminal error 进入统一 AttentionMessage；
- provider 自动重试期间不通知，终态才通知；
- bridge 重启恢复 turn/message refs 或明确 finalize abandoned progress；
- message edit failure 降级为新过程卡，但不重复 final。

验收：重启、API 限流、旧 message 不可编辑、CardKit timeout 均不会刷屏或丢最终答复。

### Phase 4：高级平台能力与偏好

- 飞书 CardKit 最终文字 streaming 灰度；
- 评估 Telegram `sendRichMessageDraft`；
- 可配置展示级别：`result_only` / `compact`（默认）/ `verbose`；
- WebUI 提供完整事件时间线，IM 过程卡可链接到 Web/tmux 观察入口。

## 推荐默认配置

```text
TMUXBOT_IM_PRESENTATION=compact
TMUXBOT_IM_PROGRESS_DELAY=4s
TMUXBOT_IM_PROGRESS_UPDATE_INTERVAL=2s
TMUXBOT_IM_PROGRESS_MAX_STEPS=3
TMUXBOT_IM_RESULT_STREAMING_TELEGRAM=0
TMUXBOT_IM_RESULT_STREAMING_FEISHU=0  # CardKit 灰度后开启
```

模式语义：

- `result_only`：除 waiting/error 外不创建过程卡；
- `compact`：默认，1 个过程卡 + 1 个结果卡；
- `verbose`：用于调试，仍尽量 edit 而非新增消息。

## 风险

1. **事件语义不足**：工具文本是 provider-specific；需要先做安全的确定性摘要器，不能靠字符串随意截断。
2. **流式 final 路径分裂**：当前 Telegram `edit_reply_stream(final=True)` 直接编辑原始 HTML，绕过统一 `ReplyDocument`；应在 Phase 2 收口。
3. **消息 edit 失败**：Telegram flood、飞书卡过期/超限必须有降级策略和 receipt，不能静默。
4. **重启状态**：只放内存的 message ID 会丢失；第一版可接受放弃旧 working 卡，但最终消息去重必须沿用 transcript offset/event id。
5. **通知语义**：更新已有消息通常不会像新消息一样产生同等通知；这正适合过程卡，但最终结果必须新发。
6. **过程卡信息过少**：不能退化成永远一句“工作中”；阶段、计数和最近关键进展必须可理解。

## 成功指标

建议用真实 route 回放衡量：

- 普通成功 Turn：文本消息 p95 <= 2；
- tool/plan 事件数量与新增消息数量解耦；
- 最终结果重复率 = 0；
- 最终结果送达率维持当前 offset-confirmed 语义；
- 过程卡 edit 失败不导致 Turn 失败；
- 用户必须操作的 waiting/error 到达率 = 100%；
- 结果正文、附件、路径、provider footer 在 TG/飞书语义一致。

## 最终建议

先做 Phase 1，不要先追逐 Telegram rich draft 或把飞书 CardKit 开到高频。最大收益来自统一语义：**一个 Turn 一个过程投影，最终结果独立发布**。平台 API 只是投影手段；如果不先收口 `jsonl.py` 中分裂的生命周期，再强的卡片 API 也只会把混乱刷得更丝滑。
