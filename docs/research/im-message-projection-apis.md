# IM 工作状态投影 API 调研：Telegram 与飞书

日期：2026-08-15  
范围：核实能否在**每个 agent turn**内维护一条可变的“工作状态”消息/卡片，并将实质性的中间或最终助手文本另发。本文是设计依据，不是实现方案或 API 调用记录。

## 结论

两端都能实现“状态投影与结果分离”：创建一条由 bot/app 自己发送的状态载体，持久化其平台消息标识，在该 turn 内仅编辑它；助手结果则另发一条普通消息/卡片（或附件）。

- **Telegram**：用 `sendMessage` 建立纯文本状态消息，之后以 `editMessageText(chat_id, message_id, text)` 覆写；最终结果用新的 `sendMessage`。这是低复杂度、跨客户端的共同基线。`editMessageMedia` 只能在状态载体确实要改成媒体时使用，不是文字进度的替代品。
- **飞书**：普通 interactive 卡可通过 IM Message `PATCH` 更新；如果要高频、渐进式文字，使用 **CardKit 卡片实体**：创建实体 → 只发送一次 → 按同一 `card_id` 和严格递增的 `sequence` 更新文本/卡片 → 关闭 streaming。最终结果另发 IM 文本、富文本或独立完成卡。CardKit 是能力更强但状态与生命周期约束更多的路径。

## 平台对比（已确认的 API 行为）

| 维度 | Telegram Bot API | 飞书 / Lark Open Platform |
| --- | --- | --- |
| 建立工作状态 | `sendMessage` 返回 `Message`/`message_id`；论坛话题发送使用 `message_thread_id`。 | `POST /im/v1/messages`，`msg_type: interactive` 可发送卡；响应返回 `message_id`。CardKit 路径为 `POST /cardkit/v1/cards` 创建实体后，以 `msg_type: interactive` 和 `{"type":"card","data":{"card_id":…}}` 发送。 |
| 更新工作状态 | `editMessageText` 用 `chat_id + message_id` 编辑文本，成功返回编辑后的 `Message`（非 inline）或 `True`（inline）。 | 普通文本/富文本：`PUT /im/v1/messages/:message_id`；普通卡：IM Message `PATCH`；CardKit：完整卡 `PUT /cardkit/v1/cards/:card_id`，或组件/文本更新。 |
| 文本流式能力 | `editMessageText` 是覆写式编辑；当前 API 也提供 `sendRichMessageDraft`，但本文的状态卡需求无需依赖新 Rich Message API。 | `PUT /cardkit/v1/cards/:card_id/elements/:element_id/content` 接受**完整当前文本**。旧文本是新文本前缀时，客户端以打字机效果追加；否则整段直接替换。 |
| 独立最终/中间结果 | 再调用 `sendMessage`（同一 `message_thread_id`）即可；状态与结果是不同 `message_id`。 | 再调用 Create Message，或在话题根消息上 Reply Message（`reply_in_thread: true`）；状态卡与结果消息是不同 `message_id`。 |
| 主要节流 | 单 chat 建议不超过 1 条/秒；群组不超过 20 条/分钟；超限会收到 429。官方 FAQ 表述针对发送/广播，未单列 edit 配额，因此编辑也必须以 429/`retry_after` 为准并合并更新。 | IM send/reply 同一用户或同一群组为 5 QPS（群组由机器人共享）；各相关 OpenAPI 为 50 QPS、1000/min。CardKit 另有**单卡**卡片/组件 API 10 次/秒上限。 |
| 载体硬限制 | `editMessageText.text`：实体解析后 1–4096 字符。 | IM 文本请求体最大 150 KB；卡片/富文本请求体最大 30 KB。CardKit 流式内容字段最大 100,000 字符，但最终卡总大小仍须不超过 30 KB；JSON 2.0 单卡最多 200 元素/组件。 |

## Telegram：可用操作与约束

### `editMessageText`

官方 [Bot API — editMessageText](https://core.telegram.org/bots/api#editmessagetext) 规定：

- 用 `chat_id`、`message_id` 定位普通聊天消息（inline 消息改用 `inline_message_id`）；新 `text` 为实体解析后 **1–4096** 字符。普通消息成功返回该 `Message`，所以首次发送返回的 `message_id` 是状态投影的关键持久状态。
- API 允许编辑文本、rich 和 game message；可保留/替换 inline keyboard。API 总则同时说明，目前仅可编辑无 `reply_markup` 或仅有 inline keyboard 的消息。
- 48 小时限制只特别适用于“代表 business account 发送、非 bot 发送且无 inline keyboard”的 business message。本文的常规 bot 自发状态消息不应错误套用该 48 小时规则。
- 对论坛话题，发送 `sendMessage` 时应带 `message_thread_id`；之后编辑使用目标 `chat_id + message_id`。最终消息重新带相同 `message_thread_id`，从而不把结果发到话题外。

**速率与失败：**官方 [Bot FAQ — broadcast limits](https://core.telegram.org/bots/faq#my-bot-is-hitting-limits-how-do-i-avoid-this) 要求单 chat 避免多于 1 条/秒、群内不多于 20 条/分钟，并说明超限会得到 429；[`ResponseParameters.retry_after`](https://core.telegram.org/bots/api#responseparameters) 提供可重试前等待秒数。因此状态投影应是“最新状态合并 + 至少按 chat 节流 + 依 `retry_after` 延后”，而不是对每一个 token/tool event 发一条新消息。

### `editMessageMedia`

官方 [Bot API — editMessageMedia](https://core.telegram.org/bots/api#editmessagemedia) 能编辑 animation/audio/document/live photo/photo/video，或将 text/rich message 替换为媒体。约束包括：

- 相册中只能改为对应允许的类型（audio 相册只能 audio，document 相册只能 document，其余只能 photo/live photo/video）。
- 编辑 inline message 不能上传新文件，只能引用已有 `file_id` 或 URL。
- 同样仅对上述非 bot business message 情形有 48 小时限制。

这足以支持“把状态消息改成完成图/媒体”的可选展示，但不适合不断变化的工作文字；文字状态应选 `editMessageText`，以避免媒体类型和上传约束。

## 飞书 / Lark：可用操作与约束

### 普通消息与普通 interactive 卡

- [创建消息](https://open.feishu.cn/document/server-docs/im-v1/message/create)：`POST /open-apis/im/v1/messages`，支持 `text`、`post`、`interactive` 等类型，响应返回后续维护需要的 `message_id`。同一用户和同一群的发送上限均为 **5 QPS**；接口级上限为 **1000/min、50/s**。应用必须启用 Bot 能力，且在群中有发言权。
- 消息创建请求限制为文本 **150 KB**、卡/富文本 **30 KB**；卡模板时模板数据也计入，样式标签会增加实际大小。
- [编辑消息](https://open.feishu.cn/document/server-docs/im-v1/message/update) 是 `PUT /open-apis/im/v1/messages/:message_id`，只支持 `text`、`post`，最多编辑 **20 次**，且只能编辑当前操作者发送的消息。已撤回/删除、超过企业管理员配置的可编辑时限、密聊/第三方加密群均不能编辑。卡片不应走此接口；官方明确要求卡片使用 Message PATCH。
- 对普通 interactive 卡，官方 [编辑应用发送的卡片](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/im-v1/message/patch) 是对应路径；其卡限制仍是 30 KB。此路线适合低频状态摘要，不具有 CardKit 的单元素打字机流式语义。

### CardKit：一张可变工作卡

[飞书流式卡指南](https://open.feishu.cn/document/cardkit-v1/streaming-updates-openapi-overview) 明确把 AI 输出列为使用场景。完整生命周期如下：

1. 使用 [Create Card Entity](https://open.feishu.cn/document/cardkit-v1/card/create) 创建 **JSON 2.0** 卡片实体（需要 `cardkit:card:write`），配置 `streaming_mode: true` 和 `update_multi: true`，记录 `card_id`。
2. 通过 IM Create Message 以 `interactive` 发送 `card_id`；官方限制：**一个卡片实体只能发送一次**，发送者应用必须与创建者相同。
3. 对固定 `element_id` 调 [Stream Updating Text](https://open.feishu.cn/document/cardkit-v1/card-element/content)：`PUT /open-apis/cardkit/v1/cards/:card_id/elements/:element_id/content`。`content` 是完整文本而非 delta；每次带唯一/幂等 `uuid` 和对该卡**严格递增**的正 `sequence`（`1..2^31-1`）。文本更新目标仅限 `plain_text` 或 `markdown`；builder 卡仅支持 markdown 富文本组件。
4. 结束时以 [Update Card Entity](https://open.feishu.cn/document/cardkit-v1/card/update) 或更新设置把 `streaming_mode` 关掉。官方建议显式关闭；未关闭会在最后一次激活后 **10 分钟**自动关闭。关闭前，流式卡不能转发，交互回调到达时也不能立即更新；应先关闭 streaming 再处理回调。

**CardKit 的硬边界：**

- 所有卡片/组件接口：50 QPS、1000/min；同一卡实体的卡片/组件操作最多 **10/s**。因此应把短促事件合并并为每卡串行化，而不直接按每个 provider delta 调用。
- `sequence` 必须对同一卡严格递增；不连续/倒退会报 `300317`。`update_multi: false` 会阻止 streaming update。
- 卡实体有效期为 **14 天**；卡大小最大 **30 KB**。全卡更新只支持 JSON schema 2.0，最多 200 个元素/组件。调用 CardKit 的 app 身份必须与创建者相同；进行中的卡交互会导致 `200810`，需要重试/稍后刷新策略。
- Stream-text 单次 `content` 最大 100,000 字符，但这是字段上限，不放宽 30 KB 卡总量；代码块两侧空格会造成渲染失败，官方要求移除。

### 话题 / thread 结果投递

官方 [Reply a Message](https://open.feishu.cn/document/server-docs/im-v1/message/reply) 是 `POST /im/v1/messages/:message_id/reply`。它可发送 `interactive`、`text`、`post` 等，并支持 `reply_in_thread: true`；若所回复消息已经在线程中，默认在线程中。响应返回 `message_id`、`root_id`、`parent_id`、`thread_id`。这意味着状态卡与最终文本都必须基于同一个已验证的 thread root/anchor 发送；不能仅靠 chat ID 推断话题位置。该接口也有 5 QPS 同用户/群限制、50 QPS/1000-min 接口限制，以及“群不支持 thread / topic 不存在”错误条件。

## 与 tmuxbot 现有概念的证据化对应

以下是仓库现状，不是额外 API 能力推断：

- `tmuxbot/frontends/base.py` 已区分 `send_status_html`/`finalize_status_html` 与 `send_reply_stream_start`/`edit_reply_stream`；`ReplyEnvelope`（`tmuxbot/core/replies.py`）是独立的助手结果载体。
- `tmuxbot/jsonl.py` 的 tool aggregator 已以 binding 名保存单个 `msg_id`，初次调用 `send_status_html`，其后调用 `edit_html`；`tmuxbot/heartbeat.py` 对 OMP compaction 状态同样保存并编辑 `msg_id`。这与“一 turn 一个 working projection”的平台 ID 模型一致，但不证明当前生命周期已严格按 agent turn 隔离。
- Telegram frontend 已声明 `supports_edit=True` 与 `max_text_length=4096`，并以 `bot.edit_message_text(chat_id, message_id, text)` 更新状态；其最终 stream 逻辑在最后一步会把溢出内容改为另发消息。见 `tmuxbot/frontends/telegram.py`。
- 飞书 frontend 已发送 JSON 2.0 interactive 卡，普通编辑用 IM Message PATCH；其 `FeishuStreamingSession` 已保存 `card_id`、`element_id`、`sequence` 并要求新内容以旧内容为前缀。它还实现 CardKit 创建、文本更新和关闭。见 `tmuxbot/frontends/feishu.py`、`tmuxbot/frontends/feishu_streaming.py` 和 `tmuxbot/frontends/feishu_cards.py`。卡构造目前已设 `schema: "2.0"`、`streaming_mode` 和 `update_multi: true`，与 CardKit 必要条件相符。
- 飞书现有出站线程路径会在有 `thread_id` 时要求已保存的 reply anchor，并调用 reply 发送；这是上述 Reply API 的必要前提，不应为状态投影移除。

## 供设计决策直接采用的实现含义（建议，不是已确认的代码行为）

1. **建立显式 turn 投影记录。**以 `(binding, agent-turn-id)` 为 key，保存 `chat_id`、`thread/root anchor`、平台 `message_id`；飞书 CardKit 额外保存 `card_id`、`element_id`、`next_sequence`、`streaming_open`。一次只允许一个活动工作投影；final/text 不复用这个 ID。
2. **选“状态摘要”而不是“全文重放”。**Telegram 4096 字符与飞书 30 KB 是载体边界。工作卡只保留最新阶段、有限的最近动作、耗时和错误摘要；完整工具输出、长助手文本与附件走独立结果消息/文件。
3. **统一合并器和背压。**按 route/card 单写者串行化，丢弃被新状态覆盖的旧更新。Telegram 至少以每 chat 1 Hz 为默认安全阈值并遵从 429 `retry_after`；飞书发送不超过 5 QPS/群，CardKit 同卡不超过 10/s，且遵从接口 50 QPS/1000-min。具体刷新间隔是本项目策略，不能声称为平台保证。
4. **完成顺序。**先停止/最终化状态投影（飞书 CardKit 显式关闭 streaming 并使用下一 sequence），再另发最终/中间助手内容；即使最终发送失败，工作卡也应进入明确 error/complete 状态，避免长期显示“工作中”。
5. **将飞书能力降级为两档。**默认可用路线是“普通 interactive 状态卡 + PATCH”；仅在 app 已获 `cardkit:card:write`、客户端 JSON 2.0 兼容可接受、并能维护 `card_id`/sequence 时启用 CardKit。若创建/更新失败，发送一次不可变的状态/结果消息，不能无界重试。
6. **不要用 Telegram media edit 实现进度。**除非产品确实需要从文本状态换成媒体，使用 `editMessageText`；媒体编辑只会增加上传、album 和 inline 限制。

## 一手资料

- Telegram: [Bot API](https://core.telegram.org/bots/api)（[editMessageText](https://core.telegram.org/bots/api#editmessagetext)、[editMessageMedia](https://core.telegram.org/bots/api#editmessagemedia)、[ResponseParameters](https://core.telegram.org/bots/api#responseparameters)）
- Telegram: [Bot FAQ — broadcast limits](https://core.telegram.org/bots/faq#my-bot-is-hitting-limits-how-do-i-avoid-this)
- 飞书: [Create Message](https://open.feishu.cn/document/server-docs/im-v1/message/create)
- 飞书: [Update Message](https://open.feishu.cn/document/server-docs/im-v1/message/update)
- 飞书: [Reply a Message](https://open.feishu.cn/document/server-docs/im-v1/message/reply)
- 飞书: [Streaming card updates](https://open.feishu.cn/document/cardkit-v1/streaming-updates-openapi-overview)
- 飞书: [Stream Updating Text](https://open.feishu.cn/document/cardkit-v1/card-element/content)
- 飞书: [Update Card Entity](https://open.feishu.cn/document/cardkit-v1/card/update)
