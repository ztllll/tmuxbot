# CHANGELOG

本文件按时间逆序记录项目主要变更,遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 格式。

---

## [2026-05-30] 默认开 1M 上下文 + /init idle-kill 默认收紧

### Changed

- **`START_CMD` 默认开 1M 上下文变体**(`claude_code.py`,commit 21e99a2):默认模型从 `claude-opus-4-8` 改为 `'claude-opus-4-8[1m]'`(单引号防 shell glob 展开;`[1m]` 启用 1M 上下文,普通 `claude-opus-4-8` 为 200K)
- **`/init` 自助开通 binding 的 idle-kill 默认从 1800 降到 600**(`provision.DEFAULT_IDLE_KILL_SECONDS`,commit d3b7d2e):闲置 10 分钟自动杀,来消息时 `--resume` 重生(上下文不丢)。手动配置的 binding 默认仍为 `idle_kill_seconds=0`(永不杀)

---

## [2026-05-29] 飞书前端 + 多通道架构 + 多实例支持

### Added

- **飞书前端 (`frontends/feishu.py`)**:通过 `lark-oapi` WebSocket 长连接接入飞书。
  - 内部 HTML → 飞书 Markdown 转换(`_html_to_feishu_md`),覆盖 `<b>/<i>/<s>/<code>/<pre>` 全集
  - 消息以 interactive card 形式发送(设 `update_multi=True`),支持 PATCH 就地编辑(工具调用聚合器复用)
  - 收到消息立即打 👀 reaction(emoji_type=`OnIt`)即时已读 ack
  - ACL 双重门禁:sender `open_id` 在 Boss 白名单,且 `chat_id` 在本前端 bindings 子集;未配置的 source 打印提示日志后**完全静默**
  - 飞书无 typing 状态 API,`send_chat_action` 为 no-op
  - 未配置 source 时打印 `chat_id` 便于接入新群/私聊

- **共享命令分发层 (`dispatch.py`)**:Telegram 与飞书共用同一套命令逻辑,不再各写一份。
  - 覆盖 stop 命令(`/esc /cc /eof`)、capture 类命令(`/context /cost /compact /clear` 等)、`/screen /info /restart`
  - `/rename` pending 态:下一条文本自动作为新名字,超时 120s 失效
  - `bot_username` 参数供 Telegram group 内剥离 `@bot_suffix`,飞书不传(None)
  - `telegram.py on_text` 改调 `dispatch_incoming_text`,行为不变

- **`Binding` 新字段**:
  - `channel: str = "telegram"`:前端渠道标识(`telegram` / `feishu`)
  - `chat_id: int | str`:Telegram 用 int,飞书用 `str`(oc_xxx 格式)
  - `idle_kill_seconds: int = 0`:>0 才 opt-in idle-kill;0 = 永不杀(默认,保护自指开发会话)

- **Idle-kill watcher (`idle.py`)**:binding 级闲置自动杀 claude。
  - 每 60s 检查一次,`idle_kill_seconds > 0` 且 TUI 非 busy 且 pane 在跑 claude → 发双 Ctrl-C 优雅杀
  - 来消息时 `ensure_running` 自动 `--resume` 重生,jsonl 原地追加,上下文不丢
  - 默认 `idle_kill_seconds=0` 永不触发,保护自指开发会话(已端到端验证)

- **同机多实例支持**(`__main__.py`):配置路径支持 env 覆盖。
  - `TMUXBOT_DATA_DIR`:data 目录覆盖(offsets / lock / log)
  - `TMUXBOT_ENV`:`.env` 文件路径覆盖
  - `TMUXBOT_BINDINGS`:`bindings.yaml` 路径覆盖
  - 背景:lark-oapi 模块级全局 loop,单进程内跑多个飞书 app 的 ws client 会报 "loop already running"。多飞书 app 场景(如 claude-feishu / codex-feishu)各跑一个进程、独立 data 目录绕开此限制

- **pyproject.toml**:新增 `lark-oapi>=1.4` 为可选依赖;未安装时其他前端正常启动,仅在实际使用飞书前端时才报 ImportError

### Changed

- **默认模型升级**:`START_CMD` 默认模型改为 1M 上下文变体 `claude-opus-4-8[1m]`(2026-05-28 发布 opus-4-8;`[1m]` 启用 1M 上下文,单引号防 shell glob 展开,普通 `claude-opus-4-8` 为 200K)
- **`__main__.py` 按 `channel` 字段分拣**:tg bindings vs feishu bindings,各自装配对应前端;`bot_username` 在 `TelegramFrontend` 内部缓存,不再每次请求重新获取

---

## [2026-05-29] Flood 回吐修复 + `/usage` 走 OAuth API

### Fixed

- **tailer 积压保护**(`jsonl.py`):新增 `JSONL_BACKLOG_LIMIT = 512KB`。单次落盘超阈值判定为「事务式 flush 爆发」——直接跳末尾、发提示消息,不逐条回吐。
  - 根因:自指 binding(bot 监控自身开发会话的 jsonl)+ claude TUI 事务式 flush(subagent 完成后一次性 flush 数 MB,含数百条 assistant 事件)+ 旧 tailer 无积压保护 → 瞬推数百条撞 Telegram flood control
  - 止血手法:杀 bot → 删 `offsets.json` 里对应 key → 重启(直接重启无效,旧 offset 仍追积压)

- **`parse_event` 过滤 `isSidechain`**(`claude_code.py`):subagent 内部对话(task 内的 transcript)不再推送到 Telegram,只推主线程事件

### Changed

- **`/usage` 走 OAuth API**(`claude_code.py`):配额限制窗口优先走 `https://api.anthropic.com/api/oauth/usage`,拿全 5 个时间窗口(5h / 7d 各子窗口)+ 精确 `resets_at`;屏幕 parse 降级为兜底(claude TUI 屏幕只显示当前活跃窗口,5h 用量 0% 时仅 2 行不全)

---

## [2026-05-27] ACL 双重门禁 + `/compact` 元数据硬信号

### Added

- **ACL 双重门禁**(`frontends/telegram.py`):从「只查 `from_user.id`」升级为同时校验:
  1. `from_user.id` 在 Boss 用户白名单
  2. `source_key (chat_id, thread_id)` 在本前端的 bindings 子集
  未配置的 source 一律完全静默(不打 👀 / 不 typing / 不回复 / 不警告)

### Fixed

- **`/compact` 完成判定终修**(`backends/claude_code.py` + `commands.py`):
  - 真硬信号改为 jsonl 里的 `type=system, subtype=compact_boundary` 事件,携带 `compactMetadata.preTokens / postTokens / durationMs / trigger`
  - `compact_metadata_since(since_byte) -> dict | None`:返回 dict 时直接提供 pre→post delta + 耗时 + 触发方式
  - 展示格式示例:`📉 token 132.0k → 10.5k (压缩 92%)\n⏱ 耗时 163s · 触发 manual`
  - 修复 stable 早退假阴:`expect_compact_done / expect_new_session` 时禁用屏幕 hash stable 提前退出;final check 加 5 次 × 1s 重试兜底 jsonl flush 滞后
  - `max_iters` 扩大到 360(总 362s 窗口,覆盖历史最长 compact + flush 滞后裕度)

- **`parse_context` regex 修正**:加 `[kmKM]?` 适配 0/1m 新会话用量显示

---

## [2026-05-27] `/status` 订阅配额章节 + 重置倒计时

### Added

- **`quota.py`**:直接 GET `https://api.anthropic.com/api/oauth/usage`,读 `~/.claude/.credentials.json` 的 OAuth bearer token
- **`/status` 新增 `🚦 订阅配额` 章节**:显示 5h / 7d / Opus / Sonnet / OAuth Apps 五窗口 utilization + 精确重置倒计时
- 拿不到 token(中转 / 第三方账号)时降级为「无法读取」一行

---

## [2026-05-27] 可插拔重构 (M2→M3) + Codex CLI 接入 + systemd 部署

### Added

- **`tmuxbot/` package 结构**:从单文件 `tmuxbot.py`(~1911 行)重构为模块化 package
  - `core`:state / utils / tmux / picker / jsonl / heartbeat / commands / config
  - `backends/`:Backend ABC + ClaudeCodeBackend + CodexBackend
  - `frontends/`:Frontend ABC + TelegramFrontend
- **Codex CLI 后端**(`backends/codex.py`):OpenAI Codex CLI tmux 注入 + jsonl 解析
- **双 bot 共存**:`TOKEN_TO_BACKEND` 映射,每个 bot ↔ 一个 backend;`bot_token_env` 字段路由 binding 到对应 bot
- **`deploy/systemd/tmuxbot.service`**:systemd user service,`Restart=always RestartSec=5s`,崩溃后 5s 内自动拉起;`MemoryHigh=2G MemoryMax=4G`
- **`bin/` 运维脚本**:`restart.sh`(含 3 次重试)、`stop.sh`(TERM → KILL)、`status.sh`

### Changed

- **tmux idle 检测**(`tmux.py`):paste 后轮询 capture-pane 等 TUI idle 再发 Enter,修 busy 态 race condition 导致消息合并或丢失
- **heartbeat 改用 TUI 状态行指纹**:抓含「时间 + token」的状态行做指纹变化判活跃,修 claude 长 thinking 时 jsonl 不涨导致 typing 闪烁

---

## [2026-05-27] M1 初版

### Added

- Telegram ↔ tmux claude TUI 双向桥骨架
- DM / 普通群 / supergroup forum topic 三场景
- 核心命令组:`/status /info /whoami /new /resume /rename /esc /cc /eof /screen /restart`
- TUI 透传命令 + capture_and_push 结构化反馈
- 工具调用聚合器:tool_use 流式刷同一条 TG 消息,真说话单独 push
- Picker 兜底:TUI picker 不可见时屏幕 OCR 推 inline keyboard
- 👀 消息已读 reaction(Bot API 7.0+)
- typing 心跳(TUI 指纹判活跃,每 4s 刷一次)
- asyncio Task 强引用修复(GC 弱引用陷阱)
- offsets.json debounced 5s 写盘
