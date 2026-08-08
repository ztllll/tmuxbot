# CHANGELOG

本文件按时间逆序记录项目主要变更,遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 格式。

---

## [Unreleased]

### Added

- systemd 安装增加 `tmuxbot install-service --now --self-heal`：单一 user service 提供开机自启动、`Restart=always` 进程恢复和周期 tmux/provider TUI 会话自愈。
- 新增精确 topic/thread route：同一 Telegram Bot 或飞书 App 可按 `(chat_id, thread_id)` 将不同话题绑定到不同 tmux pane，并按 route 选择 Claude Code、Codex 或 Pi adapter。
- 新增 Pi TUI backend：启动/恢复真实 Pi tmux TUI，解析 `~/.pi/agent/sessions` JSONL 的文本、思考、工具、模型、thinking level 和 usage；不使用 Pi RPC/SDK/print mode。
- 新增 `tmuxbot route list|inspect|validate|bind|unbind`，写入前校验完整候选配置并原子替换 YAML。
- 新增可配置的严格 Boss Admin DM route，默认 cwd 为运行账户 `Path.home()`，CLI 可选 Pi/Claude/Codex；新增 `docs/admin-dm-operations.md`，记录自然语言开通/绑定模板、确定性操作顺序、安全约束和真实 IM 验收清单。
- 新增统一 Admin Operations Contract 与 `tmuxbot admin` 事务接口：幂等安装 `AGENTS.md`/`CLAUDE.md` 受管指令，发现 routes/tmux、解析 Telegram 私有 forum 消息链接并发现飞书 topics，plan-only 创建或迁移 topic route，并在任何 target 创建前完成候选校验，在 `--apply` 中完成事务 target 创建、原子写入、supervised restart、post-apply verify 与失败回滚，使不同能力的 Admin LLM 使用同一可靠管理框架。

### Changed

- 生产部署收敛为每台主机一个 `tmuxbot.service`：同一 bridge 可承载多个 Telegram/飞书 credential；安装器会停用并删除旧的六小时 `tmuxbot-bridge-refresh@*.timer`，连接恢复改由 frontend 重连循环和 systemd supervisor 负责。
- 取消一个 credential 固定一个 backend 的限制；heartbeat、tailer、命令、附件、状态与 lifecycle 均按 binding 解析 adapter。
- Telegram 与飞书统一使用共享 ReplyDocument 展示标题、项目、route 会话、文字状态、正文和 provider footer；Telegram 混合 adapter credential 的状态 footer 也改为按当前 route 解析。
- 增强 Pi 专属状态采集，不替换 Claude/Codex 现有 parser：解析 Pi 原生 footer 的 provider、model、thinking level、累计 input/output、cacheRead/cacheWrite、最近 prompt cache hit、cost/subscription、context 百分比/窗口、auto-compact、cwd、Git branch、session name 与 extension statuses，并用当前 Pi JSONL 补全 TUI 因宽度省略的字段。Pi footer 中 `R` 按官方定义表示 cacheRead。
- `thread_id` 统一为 `int | str | None`，飞书入站提取字符串 thread，出站通过 `reply_in_thread=True` 回到原 thread；飞书 topic route 新增持久 `thread_root_message_id`，bridge 重启后或直接从 tmux TUI 对话时仍可回到精确 thread。缺少可信回复锚点时继续失败关闭，不降级到群根。
- tmux session 可被多个 route 共享，只要求完整 pane target 唯一。
- 未绑定群根/topic/thread 统一静默且不触碰 tmux；新增 route 改由 YAML、route CLI、Admin DM 或 Web control plane 显式创建。

## [0.3.0] - 2026-08-08

- WebUI 调度台增加中文分区导航、可持久化终端布局、Provider Adapter 能力声明、动态原生模型菜单，以及 Telegram/飞书群聊 @ 响应开关。
- TeamRun 协作台支持多任务依赖、统筹方案任务与实施写入任务的串行接力，并在同一界面登记证据和独立审查。
- 新增零配置 `tmuxbot serve --open`、XDG 路径、一次性首次设置授权、doctor 与 systemd user service 安装。
- 新增中文 WebUI：Provider 扫描/版本探测、项目与受管 Claude/Codex tmux 会话、Telegram/飞书配置。
- 新增 xterm.js 观察/审计接管终端，浏览器断开不终止 tmux。
- WebUI 项目向导扩展为目录验证、CLI、职责、确认四步；支持 Git/pane 发现、直接只读观察已有 pane、按职责选择 CLI，并可从原生 `/model` picker 动态选择模型。
- 新增确定性 TeamRun：三角色、DAG、单写租约、mailbox、Artifact、独立 Reviewer 验收与恢复。
- 修复飞书/Telegram 多行附件粘贴后回车竞态。

### Added

- Phase 1 Web foundation:独立 control plane 进程、首次设置与会话认证、SQLite 事件存储、只读 tmux inventory/event API。
- Tmux Runtime V2:provider/channel 归一化契约、精确 session/transcript 绑定、安全串行输入队列、Claude hooks 本地 spool、`off|shadow|on` 灰度路由。
- Claude/Codex × Telegram/飞书 2×2 回复 envelope 与真实临时 tmux pane E2E 覆盖;tmux 继续是唯一执行面。
- 项目版本与发布治理基础:版本策略、发布流程、GitHub issue/PR 模板、CI/Dependabot 工作流、贡献/安全/支持文档。
- 元数据一致性测试,防止 `pyproject.toml` 与 `tmuxbot.__version__` 漂移。
- Codex backend 回归测试,覆盖按 binding `cwd` 选择 rollout jsonl 以及无匹配时不兜底到全局最新文件。
- Codex `update_plan` 跟随消息:计划更新不再只显示 `📋 更新计划`,而是维护一条可编辑的当前计划消息,持续展示完整 step/status。
- Codex custom tool 可见摘要:补齐 `apply_patch` 调用和 `patch_apply_end` 成功/失败摘要,让 tmux TUI 里的改文件动作同步回 TG/飞书。
- 共享附件注入 helper,统一 IM 附件落盘路径、文件名清洗和 `@path` prompt 生成。
- Telegram/飞书出站附件发送:AI 回复里的本地图片/文件路径会转成原生 IM 图片/文件消息,不再只把路径贴给用户。

### Changed

- GitHub Actions 基础 action 升级到当前主版本，将 CI job 权限收紧为只读仓库内容，并加入独立 WebUI test/build job；WebUI lockfile 同步消除已知 npm audit 漏洞。
- 测试环境统一清除宿主机 `CLAUDE_BIN` / `CODEX_BIN` 覆盖，避免生产部署路径污染 provider discovery 与启动命令断言。
- tmux 会话改为默认按消息懒启动：人工关闭或 IM `/tmuxstop` 后不再被后台周期复活；Telegram、飞书和 Web 控制台都可关闭 tmux 并保留 binding/provider 历史，下一条 IM 消息自动重建并 resume。常驻自愈改为 `TMUXBOT_LIFECYCLE_ENABLED=1` 显式 opt-in。
- 受管 Codex 会话的新建与 `--resume` 恢复均读取 `~/.codex/config.toml` 的 `model` 并显式传入 `-m`；配置变更会自动用于下一次启动或恢复。
- Telegram 与飞书统一使用 `IncomingMessage` / `ReplyEnvelope`;回复尾部由 Claude/Codex provider 解析 `TerminalStatus`,不再由渠道猜测屏幕最后一行。
- Claude `Stop.last_assistant_message` 作为优先最终回复源,JSONL 保留工具、思考、用量与历史数据并对重复最终回复去重。
- `pyproject.toml` 补齐标准 package metadata、console entry point 与项目链接。
- `.gitignore` 覆盖多实例部署文件:忽略 `bindings*.yaml` 和 `data*/`,同时保留已跟踪的 `bindings.example.yaml`。
- Telegram 附件处理从仅支持图片/文档扩展到图片、文档、视频、动图、音频、语音。
- Telegram 增加 `TELEGRAM_GROUP_MENTION_ONLY` / per-token 唤醒开关,群和话题可要求显式 @bot 后才注入 tmux。
- Telegram/飞书唤醒判断统一为“私聊、@bot、回复 bot 消息”三类触发;群里回复 bot 历史消息也可进入 tmux。
- Codex `update_plan` 从普通 tool aggregator 拆到独立 `assistant_plan` 事件,后续计划更新编辑同一条 IM 消息,避免被工具日志刷走。

### Fixed

- 多飞书 App 单进程运行时不再共享并覆盖 `lark-oapi` 的模块级 event loop；每个 credential 使用独立 SDK module/worker loop，并在服务停止时显式断开 WebSocket 和关闭 loop。
- 飞书 WebSocket dispatcher 现在消费群资料更新、bot 入群、普通成员变更和消息撤回等无路由副作用事件，避免已订阅的 `im.chat.updated_v1` / `im.chat.member.*` / `im.message.recalled_v1` 被 SDK 反复记录为 `processor not found`；群解散与 bot 被移出仍沿用原有 route 拆除逻辑。
- Codex 启动路径改为在每次新建/恢复 provider 时读取 `CODEX_BIN`，不再在 Python import 阶段提前冻结，确保 bridge 加载 `.env` 后的绝对路径真正生效。
- 修复飞书流式回复完成时未把 provider 状态传入最终 CardKit 卡片、工具状态卡完成时又主动清空 footer 的问题；飞书新回复和完成卡片现在都会保留模型、档位与权限信息，并移除过期的 `working` 时长。
- 补齐 Claude Code 档位回填：当 TUI 状态栏不显示档位时，从当前绑定的活动 transcript 顶层 `effort` 读取并写入统一 `TerminalStatus`，飞书“同传系统开发”等 Claude binding 的回复末尾现在可显示 `high` 等档位。
- 修复 Codex 新版状态栏带 Git 分支后缀、工作目录为 `~`，或工作态暂时隐藏模型行时无法解析推理档位的问题；缺失的模型与档位会从当前 binding 的活动 rollout 运行时设置（`thread_settings` / `turn_context`）同次回填，Telegram/飞书回复末尾现在会显示 `low`、`medium`、`high`、`xhigh` 等当前档位。
- 修复飞书图片等多行附件已粘贴到 Claude/Codex 输入框、但首个 Enter 被 TUI 忽略后一直停留未提交的问题；现在会读取活动输入框确认提交，原草稿仍在时有限重试，CLI 已工作或输入框已清空时不会重复发送。
- Web 首次设置同时要求 loopback peer 与一次性 `X-Setup-Token`,避免同机反向代理后的远程客户端抢占初始密码。
- Telegram 多 bot 服务重启时即使 polling 已先停止,也保证关闭 aiogram HTTP session,不再残留 `Unclosed client session`。
- 修复 Codex pane 前台命令为 standalone `codex` 时被 watchdog 误判并周期性注入启动命令的问题;未知前台进程不再注入 Claude/Codex 启动命令。
- 修复输入在 pane busy 时先 paste 后等待导致多条命令堆积的问题;现在按 tmux target 排队并在粘贴前确认 idle 与前台进程。
- Codex 多 binding 串线风险:Codex rollout 路径不含 cwd,旧逻辑在找不到当前 binding 的 `session_meta.payload.cwd` 时会返回全局最新 `rollout-*.jsonl`,导致多个 chat 可能同时 tail 同一个 Codex 会话。现在只接受 cwd 匹配的 rollout,找不到就返回 `None`。
- 飞书通道补齐普通文件消息下载与注入,不再只支持图片和图文里的图片。
- Codex `update_plan` 内容回传不完整:旧逻辑只取 `in_progress` 第一项,甚至全完成时只显示标题。现在完整回传 explanation、最多 12 条计划项及状态。

---

## [2026-06-10] Claude 启动支持 CLAUDE_BIN 绝对路径

### Fixed

- **hbhy 新建 Claude 会话失败: npm 全局安装缺 native binary**。远端 `@anthropic-ai/claude-code` 缺少平台 optional dependency,新建 pane 报 `Error: claude native binary not installed`。项目启动链路新增 `CLAUDE_BIN` 运行时读取,生产可固定到 native installer 产物 `~/.local/bin/claude`,不再依赖 systemd/tmux 的 `PATH` 或坏掉的 npm 入口。
- **测试覆盖**:`tests/test_claude_code_backend.py` 验证 `CLAUDE_BIN` 在运行时生效,避免 `.env` 加载顺序回归。

### Docs

- README / DEVELOPMENT / `.env.example` 同步记录 Claude Code native install 推荐路径、`CLAUDE_BIN` 配置和 npm 安装故障红线。

---

## [2026-06-01] 修 tailer 切新会话丢首条回复(/clear /new 可靠化)

### Fixed

- **`jsonl.py` 切新会话 offset 跳末尾 → 丢首条回复**。旧逻辑:tailer 检测到 jsonl 切换(`jl != last_file`)且 `key not in offsets` 时,一律把 offset 设成 `jl.stat().st_size`(跳末尾防积压回吐)。但 **`/clear` `/new` 运行中新建会话**时,新会话首条回复常在 tailer 切过来前就落盘 → 被跳过 → Boss 收不到 `/new` 后第一条回复。
  - **修法**:区分两种「key 首见」——「初次启动」(`last_file is None` → 跳末尾,防 bootstrap 回吐历史积压)vs「运行中切到新会话」(`last_file` 已有 → 从 offset **0** 读全)。一行条件:`state.offsets[key] = 0 if last_file is not None else jl.stat().st_size`。
  - 新会话 jsonl 很小无 flood 风险;`JSONL_BACKLOG_LIMIT`(512KB)仍兜底意外大文件。
  - **意义**:Boss 的"存记忆 → `/new` 开新会话 → 接着干"工作流从此可靠(此前 `/new` 首条回复时序竞争丢失)。
  - 自指会话重启实测:`[tmuxbot]` 从已存 offset 续读、未回吐 32MB 积压、无 backlog 触发。

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
