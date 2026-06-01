# CHANGELOG

本文件按时间逆序记录项目主要变更,遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 格式。

---

## [2026-06-01] 任务 footer 改由 bot 从 TodoWrite 渲染(根治"演任务")

### Changed

- **任务 footer 数据源:claude 手写 → bot 从真实 TodoWrite 渲染**。根因:旧 §6 让 claude "手写任务面板",claude 便用散文叙述任务(实测某会话 footer 报 46 任务但 TodoWrite 只建过 3 个、line 71 后彻底废弃任务工具)——footer 与真实任务状态脱钩、且鼓励"演任务"而非"用任务模式"。
  - `claude_code.py parse_event`:遇 `TodoWrite` tool_use → 抓 `todos` 发 `("task_state", json)` 事件
  - `state.py`:加 `task_state: dict[str, list]`(binding → 最新 todos)
  - `jsonl.py`:`task_state` 事件更新状态;`assistant_text` 推送时**剥掉 claude 手写 footer** + **从真实 TodoWrite 渲染 footer 追加**
  - `utils.py`:`render_task_footer(todos)`(§6 格式)+ `strip_handwritten_footer(text)`
  - 全局宪法 §6 改:claude **只管维护 TodoWrite,不手写 footer**(手写会被剥掉)→ 逼出真任务模式、footer 永远忠实反映真实任务

---

## [2026-06-01] claude 启动去掉 --model(最新版默认即 opus-4.8 1M)

### Changed

- **START_CMD 去掉 `--model 'claude-opus-4-8[1m]'`** → `claude --dangerously-skip-permissions`(只剩最高权限免审批)。Claude Code 升级到最新版后**默认 opus 模型即 opus-4.8 1M 上下文**(Boss + 网络同传裸启动实测确认 `Opus 4.8 (1M context)`),`--model` 多余。两端启动统一为"干净 + 最高权限":claude=`--dangerously-skip-permissions`,codex=`--dangerously-bypass-approvals-and-sandbox`。auto-update timer 保证 CLI 常新 → 默认始终 1M。

---

## [2026-05-31] 下线 idle-kill 模块

### Removed

- **彻底移除 idle-kill 功能模块**:hbhy 内存已升级,不再需要"省内存式闲置自动杀进程"——该机制会打断长任务 / 定时唤醒,弊大于利,整模块下线。具体移除:
  - 删整个 `tmuxbot/idle.py`(`idle_kill_loop` 闲置自动杀 claude 的看门狗)
  - 删 `Binding.idle_kill_seconds` 字段(`state.py`)及其在 `config.py` / `provision.py` 的读取与默认赋值
  - 删 `provision.DEFAULT_IDLE_KILL_SECONDS`(连同 `/init` 自助开通 binding 写 yaml 的 `idle_kill_seconds` 字段)
  - 删 `__main__.py` 的 `idle_kill_loop` import + 启动 `S.fire(...)` 行
  - 删 `tmux.py` 的 `tmux_respawn_pane`(仅 idle-kill 兜底用,现无引用)
  - `last_active` 状态**保留不动**(heartbeat typing 指示 / 前端仍在用,非 idle 专属)

---

## [2026-05-30] codex 最高权限 + 默认开 1M 上下文 + /init idle-kill 默认收紧

### Changed

- **codex 启动加 `--dangerously-bypass-approvals-and-sandbox`**(`codex.py`,commit aae0d5d):codex 最高权限(跳过所有审批 + 无沙箱),等价 claude 的 `--dangerously-skip-permissions`。原裸跑 `codex` 默认审批+沙箱会弹审批 picker 卡住无人值守注入。`CODEX_BIN` 仍可配绝对路径,flag 始终追加
- **`START_CMD` 默认开 1M 上下文变体**(`claude_code.py`,commit 21e99a2):默认模型从 `claude-opus-4-8` 改为 `'claude-opus-4-8[1m]'`(单引号防 shell glob 展开;`[1m]` 启用 1M 上下文,普通 `claude-opus-4-8` 为 200K)。**走中转(如 sub2api/new-api)也拿真 1M**:Claude Code 客户端把 `[1m]` 别名翻译成 body `model=claude-opus-4-8` + header `anthropic-beta: ...,context-1m-2025-08-07`,中转转发该 header → 上游 1M(实测 205k tokens 输入 HTTP 200)。⚠️ 勿手动把字面 `claude-opus-4-8[1m]` 当模型名发给中转(上游 404)
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
