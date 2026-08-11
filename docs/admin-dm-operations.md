# 使用 Admin DM 管理 Telegram 话题与 tmux

Admin DM 是 Boss 与一个根目录管理 TUI 之间的严格私聊 route。它适合在手机端用自然语言要求管理 AI 检查、创建和绑定 tmux pane 与 Telegram forum topic。

```text
Boss DM
  ↕
Admin tmux pane
  ↕
Pi / Claude Code / Codex
  ↕
当前 Unix 用户权限
```

Admin AI 不是受限的配置机器人。Boss DM 通过身份与私聊形状校验后，pane 内的 CLI 拥有运行 tmuxbot 的 Unix 用户权限，可以运行 `tmuxbot route`、`tmux` 和 `systemctl --user`，也可以编辑 YAML。高层命令用于提高确定性，不是权限沙箱。

## 1. 启用 Admin DM

推荐用独立 systemd drop-in 或部署环境设置：

```ini
[Service]
Environment="PI_BIN=/home/you/.local/bin/pi"
Environment="TMUXBOT_ADMIN_ENABLED=1"
Environment="TMUXBOT_ADMIN_CHANNEL=telegram"
Environment="TMUXBOT_ADMIN_CHAT_ID=123456789"
Environment="TMUXBOT_ADMIN_CREDENTIAL=TG_BOT_TOKEN"
Environment="TMUXBOT_ADMIN_TMUX=tmuxbot-admin"
Environment="TMUXBOT_ADMIN_CLI=pi"
Environment="TMUXBOT_ADMIN_CWD=/home/you"
```

其中：

- `TMUXBOT_ADMIN_CHAT_ID` 必须是 Boss 的正数 Telegram private user ID；省略时默认使用 `BOSS_USER_ID`。
- `TMUXBOT_ADMIN_CREDENTIAL` 指定承载管理 DM 的 Telegram Bot credential。
- `TMUXBOT_ADMIN_CWD` 默认是运行用户的 `Path.home()`。建议管理会话使用用户根目录，而不是某个项目目录。
- `TMUXBOT_ADMIN_CLI` 可为 `pi`、`claude_code` 或 `codex`。
- `admin: true` YAML 记录只保存 provider session identity；没有 `TMUXBOT_ADMIN_ENABLED=1` 时不会授予 Admin 权限。

修改 systemd 配置后：

```bash
systemctl --user daemon-reload
systemctl --user restart tmuxbot.service
```

## 2. Admin Operations Contract（所有 LLM 的统一入口）

Admin LLM 不应自行拼装 YAML、tmux 和 systemd 操作。普通“创建项目/绑定项目”只使用一个高层接口 `provision-project`；topic 发现、平台创建、tmux、route、重启、verify 和 rollback 都藏在该深模块内部：

```bash
# 已有 Telegram topic：三段式 URL 已足够，tmux 默认 NAME:0.0
tmuxbot admin --file /path/to/bindings.yaml --service tmuxbot.service \
  provision-project --name pi-demo --channel telegram \
  --credential TG_CODEX_BOT_TOKEN \
  --topic-link https://t.me/c/INTERNAL_CHAT_ID/THREAD_ID \
  --cwd /absolute/project/demo --backend pi

# 新建 Telegram/飞书 topic：用精确 chat_id + 名称替换 --topic-link
tmuxbot admin --file /path/to/bindings.yaml --service tmuxbot.service \
  provision-project --name pi-demo --channel feishu \
  --credential FEISHU_CODEX --chat-id oc_xxx --topic-title "Demo 项目" \
  --cwd /absolute/project/demo --backend pi
# 两种模式都先核对 plan，再原命令增加 --apply。

# 以下是安装契约和低层诊断/恢复命令
# 打印机器可读/人可读的操作契约
tmuxbot admin --file /path/to/bindings.yaml --service tmuxbot.service contract

# 把受管契约幂等安装到 Admin cwd 的 AGENTS.md + CLAUDE.md
tmuxbot admin --file /path/to/bindings.yaml --service tmuxbot.service \
  install-contract --cwd /home/you

# 发现当前 routes 与真实 tmux panes
tmuxbot admin --file /path/to/bindings.yaml --service tmuxbot.service \
  inventory --json

# Telegram 私有 forum 的话题 URL 已足够；最后的消息 ID 可省略
tmuxbot admin --file /path/to/bindings.yaml --service tmuxbot.service \
  telegram-topic \
  --message-link https://t.me/c/INTERNAL_CHAT_ID/THREAD_ID --json

# 发现飞书群内最近的精确 topic/thread ID（只读，不发消息）
tmuxbot admin --file /path/to/bindings.yaml --service tmuxbot.service \
  feishu-topics --env-file /path/to/.env \
  --credential FEISHU_CODEX --chat-id oc_xxx --json

# 用户明确要求新建 Telegram/飞书话题时，一条事务覆盖 topic、tmux 和 route
tmuxbot admin --file /path/to/bindings.yaml --service tmuxbot.service \
  create-topic --env-file /path/to/.env --channel feishu \
  --name demo-pi --credential FEISHU_CODEX --chat-id oc_xxx \
  --topic-title "Demo 项目" --tmux-target demo-pi:0.0 \
  --cwd /absolute/project/demo --backend pi \
  --mention-required false --create-target
# 核对 plan 后，原命令增加 --apply。
```

标准事务流程固定为：

```text
provision-project（默认 plan-only）
→ 核对 endpoint intent / route / cwd / adapter / target_action
→ 原命令增加 --apply
→ 内部解析或创建精确 endpoint
→ 内部创建或复用 exact-cwd tmux target
→ 原子写 route、重启 supervised bridge、post-apply verify
→ 由 Boss 在真实 topic 发消息完成双向验收
```

LLM 不再自行决定先跑 `inventory`、`telegram-topic`、`feishu-topics`、`create-topic` 还是 `bind-topic`。这些命令继续存在，但只用于离线恢复、独立诊断和迁移；正常项目开通固定走 `provision-project`。

用户明确要求创建新的 Telegram 或飞书话题时，统一使用一体化事务：

```bash
tmuxbot admin --file /path/to/bindings.yaml --service tmuxbot.service \
  create-topic --env-file /path/to/.env --channel feishu \
  --name demo-pi --credential FEISHU_CODEX \
  --chat-id oc_xxx --topic-title "Demo 项目" \
  --tmux-target demo-pi:0.0 --cwd /absolute/project/demo \
  --backend pi --mention-required false --create-target

# plan 中的 chat/title/target/cwd/adapter 全部吻合后，再重复并加 --apply。
```

`--apply` 才会调用 Telegram `createForumTopic` 或飞书根消息接口并取得平台返回的 `thread_id`，随后创建目标、写入 route、重启 bridge 和 verify。后续步骤失败时会恢复 YAML、清理本事务新建的 tmux session，并尝试删除本事务新建的话题。不要再让 Admin LLM 自行拼 Bot API/Feishu SDK 脚本、手工启动 provider 或跨多条命令传递 thread ID。

绑定已有 topic：

```bash
tmuxbot admin --file /path/to/bindings.yaml --service tmuxbot-codex.service \
  bind-topic \
  --name demo-pi \
  --channel feishu \
  --credential FEISHU_CODEX \
  --chat-id oc_xxx \
  --thread-id omt_xxx \
  --thread-root-message-id om_xxx \
  --tmux-target demo-pi:0.0 \
  --cwd /absolute/project/demo \
  --backend pi \
  --mention-required false

# 核对 plan 后再加 --apply；目标不存在时还必须显式加 --create-target。
```

迁移已有 route 到统一项目群的新话题（保留 pane、cwd、adapter 与 provider identity）：

```bash
tmuxbot admin --file /path/to/bindings.yaml --service tmuxbot-codex.service \
  move-topic existing-route \
  --channel feishu \
  --chat-id oc_new_group \
  --thread-id omt_new_topic \
  --thread-root-message-id om_new_root

# 核对 before/after 与 preserves 列表后再加 --apply。
```

独立验证：

```bash
tmuxbot admin --file /path/to/bindings.yaml --service tmuxbot-codex.service \
  verify existing-route --json
```

已有飞书 topic 的 `--thread-root-message-id` 取自 `feishu-topics` 输出的 `root_message_id`；新 topic 则由 `create-topic --channel feishu --apply` 自动取得并写入。它是稳定出站锚点：bridge 重启后，即使用户直接在 tmux TUI 对话，回复仍可通过 `reply_in_thread=True` 返回精确 thread；缺失时 Admin 事务会在任何 tmux/YAML/systemd 副作用前拒绝。provider TUI 不按空闲时间退出；服务默认每小时仅巡检已存在 pane 的健康状态。

`--apply` 事务会校验完整候选 route table，并使用原子 YAML 替换。systemd 重启或 post-apply verify 失败时恢复旧 YAML 并尝试恢复旧 bridge。若本次显式创建了新 tmux session，事务失败会清理该新 session；不会触碰预先存在的 tmux。

`provision-project` 默认把 route 名作为 tmux session 名并使用 `NAME:0.0`；可用 `--tmux-target` 显式覆盖。目标不存在时 `--apply` 自动事务创建，不再要求操作者额外记 `--create-target`；目标已存在时只在 cwd 完全相同且未被 route 占用时复用。禁止为 route 直接运行 `tmux new-session`。消息懒启动保持不变：新建 pane 初始可为 shell，第一条 IM 消息由 route adapter 启动真实 TUI。

## 3. 在 DM 中需要提供什么

自然语言请求最好包含以下信息：

1. Telegram 群名或 `chat_id`；
2. 已有话题的 `thread_id`，或该话题内一条消息的链接；
3. 项目工作目录 `cwd`；
4. 使用的 adapter：`pi`、`claude_code` 或 `codex`；
5. 绑定已有 tmux target，还是创建新的 session/window/pane；
6. 群内是否需要 `@bot`。项目话题通常明确写“无需 @机器人”，对应 `mention_required: false`；
7. 是否允许创建新 Telegram/飞书话题。若要求绑定已有话题，应明确写“不要新建话题”；若明确要求创建话题，只需提供精确 `chat_id`，不需要先由用户手动创建再回传 thread ID。

## 4. 常用自然语言模板

### 4.1 绑定已有话题到已有 pane

```text
请把 Telegram 群「项目群」的已有话题 8024
绑定到 tmux pane project-pi:0.0。

配置：
- credential: TG_BOT_TOKEN
- cwd: /home/you/projects/project
- adapter: pi
- 群里无需 @机器人
- 不要新建 Telegram 话题

操作前检查现有 route，不要修改无关 binding。
修改后 validate 配置，重启 tmuxbot.service，
并汇报 endpoint、tmux target、pane command、cwd 和 tailer 状态。
```

### 4.2 没有 tmux，创建后绑定已有话题

```text
请为 /home/you/projects/demo 创建 Pi tmux，
然后绑定到 Telegram 群「项目群」的已有话题 12345。

要求：
- tmux session: demo-pi
- target: demo-pi:0.0
- adapter: pi
- credential: TG_BOT_TOKEN
- 群里无需 @机器人
- 不要新建 Telegram 话题

完成后校验 route，重启 bridge，检查 Pi、JSONL tailer 和 polling。
```

### 4.3 在已有 session 中增加 window/pane

```text
请在 tmux session project-team 中创建一个新的 window，
cwd=/home/you/projects/new-project，启动 Pi，
并绑定到 Telegram 群「项目群」的已有话题 12345。

自动选择未被 route 占用的 window/pane，
设置 mention_required=false，不要新建 Telegram 话题。
```

### 4.4 确实需要同时创建 Telegram 话题

```text
请在 Telegram 群「项目群」新建话题「Demo 项目」，
创建 tmux session demo-pi，cwd=/home/you/projects/demo，
使用 Pi，并把新话题绑定到 demo-pi:0.0。

使用 TG_BOT_TOKEN，新话题无需 @机器人。
完成后汇报新 thread_id 并验证双向路由。
```

只有在请求明确允许时才应创建 Telegram/飞书新话题，并统一使用 `tmuxbot admin create-topic` 的 plan/apply 事务；绑定已有话题时不得因为缺少 thread ID 而擅自新建。

### 4.5 只提供 Telegram 消息链接

私有 supergroup/forum 的话题链接有两种可接受形式：

```text
https://t.me/c/<internal-chat-id>/<thread_id>
https://t.me/c/<internal-chat-id>/<thread_id>/<message_id>
```

绑定 route 只需要前两个 ID；`message_id` 不是 Telegram topic route 字段，也不需要 `thread_root_message_id`。

可以在 DM 中说：

```text
请把这个已有 Telegram 话题绑定到项目 Pi：
https://t.me/c/xxxxxxxxxx/12345/67890

目标：
- tmux: demo-pi:0.0
- cwd: /home/you/projects/demo
- adapter: pi
- 无需 @机器人
- 不要新建话题
```

使用以下确定性命令解析私有 forum 链接：

```bash
tmuxbot admin --file /path/to/bindings.yaml --service tmuxbot.service \
  telegram-topic \
  --message-link https://t.me/c/xxxxxxxxxx/12345/67890 --json
```

它会把 `internal-chat-id` 转换为 Bot API `chat_id=-100...`，并返回整数 `thread_id`；如果 URL 还包含具体消息 ID，也会附带返回。三段式话题 URL 与四段式消息 URL 都有效；public username 链接、群根链接或缺少 thread 的链接会失败关闭。如果链接或名称不足以可靠识别 endpoint，管理 AI 应要求提供 `chat_id/thread_id`，而不是猜测或创建新话题。

## 5. 确定性底层流程

管理 AI 的正常顺序只有：

```text
1. tmuxbot admin provision-project ...        # plan
2. 核对 endpoint intent、cwd、adapter、target action
3. 原命令增加 --apply                          # 内部 create/bind/restart/verify
4. 由 Boss 在真实 topic 发消息验收双向链路
```

只有高层命令尚未覆盖的操作（例如已有 tmux session 新增 window/pane）才允许使用底层 `tmux`，且必须回到 `bind-topic plan → --apply → verify` 收口。直接编辑 YAML 仍是离线恢复能力，不是普通 Admin LLM 的首选路径。

当前 `tmuxbot route` 第一阶段支持：

```text
tmuxbot route list [--json]
tmuxbot route inspect NAME [--json]
tmuxbot route validate
tmuxbot route bind ...
tmuxbot route unbind NAME
```

尚未实现进程内 hot reload，因此 YAML 变化后需要重启受监督的 bridge。不要执行 `tmux kill-server`；它会影响所有用户 tmux 会话。

## 6. 重要安全和路由约束

- Admin 权限只对 Boss 身份和 Telegram private chat 同时成立；群中 `@bot`、`/panel` 或伪造 route 名不能取得 Admin 权限。
- 未配置的群根、topic 和 thread 完全静默，不打反应、不 typing、不回复，也不触碰 tmux。
- endpoint 唯一键是 `(channel, bot_token_env, chat_id, thread_id)`。
- tmux target 唯一键是 `(tmux_session, tmux_window, tmux_pane)`；同一个 tmux session 可以承载多个不同 pane route。
- 一个 credential 可以混合承载 Claude Code、Codex 和 Pi；adapter 是 route 属性，不由 Bot token 决定。
- 项目话题应绑定项目 pane；Admin DM 应绑定用户根目录下的独立管理 pane，不应复用项目 pane。
- 同一 backend 不应让两个 route 竞争同一 cwd transcript。需要并行独立会话时，应使用不同项目/worktree cwd 或明确的 provider session 隔离策略。
- Pi 建议 tmux 开启：

  ```tmux
  set -g extended-keys on
  set -g extended-keys-format csi-u
  ```

  `tmuxbot doctor` 只诊断，不自动重启 tmux server。

## 7. 验收清单

每次开通或改绑后至少确认：

```text
[ ] tmuxbot route validate 通过
[ ] route inspect 显示正确 chat_id/thread_id/credential
[ ] pane_current_command 是预期 CLI
[ ] pane_current_path 是预期 cwd
[ ] systemctl --user is-active tmuxbot.service = active
[ ] polling 日志显示正确 Bot
[ ] 对应 binding 的 tailer alive
[ ] 项目 topic 普通消息是否按预期需要/不需要 @bot
[ ] DM 消息只进入 Admin pane
[ ] topic 消息只进入对应项目 pane
[ ] assistant 回复回到同一个 DM/topic
[ ] 群根和未绑定 topic 保持静默
```

## 8. 示例：推荐的管理/项目布局

```text
Boss DM
└─ tmuxbot-admin:0.0
   cwd=/home/you
   adapter=pi
   用途=管理 tmux、route、systemd 与项目入口

统一项目群
├─ 话题 8024 → project-a:0.0 → /home/you/projects/a → pi
├─ 话题 9001 → project-b:0.0 → /home/you/projects/b → claude_code
└─ 话题 9002 → project-c:0.0 → /home/you/projects/c → codex
```

用户在电脑上仍可通过 `tmux attach` 接管同一 pane；Admin DM 和项目话题都只是这些真实 TUI 的远程入口。
