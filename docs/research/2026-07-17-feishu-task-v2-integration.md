# 飞书任务（Task V2）接入可行性

日期：2026-07-17 UTC
范围：仅核实飞书开放平台官方 Task V2 API 是否能把 tmuxbot / TeamRun 的任务同步到飞书任务。

## 结论

可行。Task V2 可创建任务、设置负责人/关注人、更新完成状态，并通过事件订阅感知任务变化。第一版适合把 tmuxbot 的 `TeamTask` 映射为一个飞书任务，并在本地保存 `task_guid`；不要把飞书的 `todo/done` 当作 TeamRun 全部状态机的替代品。

## 官方能力与限制

| 需求 | 官方 API / 事件 | 结论 |
| --- | --- | --- |
| 创建任务 | [创建任务](https://open.feishu.cn/document/task-v2/task/create) `POST /open-apis/task/v2/tasks` | 支持标题、描述、清单、开始/截止时间及 `members`；创建接口支持 `client_token` 幂等。需申请 `task:task:write` 或 `task:task:writeonly`。 |
| 指派负责人 | [任务功能概述](https://open.feishu.cn/document/task-v2/task/overview)、[添加任务成员](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/task-v2/task/add_members) | 任务成员 `type` 可为 `user` 或 `app`，角色可为 `assignee` 或 `follower`。因此应用/机器人可作为任务成员和负责人；实际调用者还必须有该任务编辑权限。人员型自定义字段仅支持 `user`，不能用来放机器人。 |
| 更新状态 | [更新任务](https://open.feishu.cn/document/task-v2/task/patch) | `PATCH /open-apis/task/v2/tasks/:task_guid`，将 `completed_at` 加入 `update_fields`：非零毫秒时间戳表示完成，`"0"` 恢复未完成。Task V2 的公开状态只有 `todo` / `done`；OpenAPI 只能整体完成，不支持逐个负责人完成。 |
| 订阅变化 | [任务更新事件](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/task-v2/task/events/update_user_access) | 事件 `task.task.update_user_access_v2` 可报告创建、删除、负责人、完成状态、标题、描述、关注人、提醒、开始/截止时间变化；官方页面标明 Webhook 推送。订阅前须在开发者后台配置订阅方式并添加事件，且需要 `task:task:read`。 |
| 主动订阅范围 | [订阅任务更新事件](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/task-v2/task_v2/task_subscription) | 可用应用身份订阅应用负责的任务，或用用户身份订阅该用户创建、负责、关注的任务；调用前同样需要开发者后台事件配置。 |

## 建议的 tmuxbot 映射

1. 仅在 TeamRun 创建后异步创建飞书任务；`client_token` 使用稳定的 `teamrun_id + team_task_id`，本地保存 `task_guid`。
2. 创建时将操作者设为 `user` `assignee`，将飞书应用设为 `app` `follower`（或在业务确认后设为 `app` `assignee`）。不要把 CLI/LLM 当作飞书用户身份伪造。
3. 本地 `accepted`/`completed` 才 PATCH `completed_at`；`working`、`review`、`blocked` 等更细状态写进任务描述、`extra` 或注释，而不是丢失在 `todo/done` 中。
4. 接到事件后以 `task_guid` 回查任务详情，再按幂等事件记录同步；用户将任务标记完成可请求 TeamRun 进入“待核验”，不要直接绕过审查完成本地任务。

## 实施前置条件

- 飞书应用需开通并经管理员同意 `task:task:write`（以及接收事件所需的 `task:task:read`）权限。
- 需要一个可被飞书回调的 Webhook，或采用飞书官方 SDK 支持的长连接事件订阅；现有仅出站消息的通道配置不足以接收 Task 事件。
- 先用租户测试环境验证：应用作为 `app` 类型 `assignee` 是否符合本租户任务中心展示及权限策略；官方 API 的成员类型允许该组合，但产品展示/治理要求应由管理员确认。
