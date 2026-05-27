# tmuxbot — 项目宪法

> 全局协作准则见 `~/.claude/CLAUDE.md`。本文件只放 **tmuxbot 项目特有**的铁律与事实。
> AI 加载顺序: 全局 CLAUDE.md → 本文件 → `~/.claude/projects/<cwd>/memory/MEMORY.md`。

---

## 0. 项目角色

- **目标**: Telegram ↔ tmux Claude Code TUI 双向桥。Boss 在 TG 任意端点(DM / 群 / forum topic)发消息 → 自动注入对应 tmux pane 的 claude → claude 回复实时回推同端点。
- **不调 Claude API**: bot 只搬键盘 + 屏幕,不消费 token。token 全在 claude TUI 那一头花。
- **当前状态 (2026-05-27)**: M1 完成,准备进入 M2 (可插拔重构 + 接入 codex cli)。`tmuxbot.py` 单文件 ~2017 行。

---

## 1. 自我审查要求 (★ 每次改完必走)

**任何修改完毕后,主动进行自我评估和审查**,作为收尾的一部分。审查清单:

### A. 代码逻辑

- [ ] import 链完整,没有遗漏新加依赖
- [ ] 字段命名一致 (State 加字段 → 所有使用点更新)
- [ ] 错误处理对称 (try → except → finally / 资源释放)
- [ ] 函数家族对齐 (parse_X 系列、cmd_X 系列、tmux_X 系列内部约定一致)
- [ ] 没有半完成的 stub / TODO 残留

### B. 文档一致性

- [ ] prompt 文本 vs parser 字段对齐
- [ ] BotCommand 注册 vs handler 实现对齐
- [ ] DEVELOPMENT.md 描述 vs 真实代码对齐
- [ ] 改了路径 / 命名 → 更新本宪法和 DEVELOPMENT.md

### C. 真跑

- [ ] 不是 `ast.parse` / `py_compile`,而是真启动 bot
- [ ] 双 binding (DM + group topic) 都各发一条消息验证
- [ ] log 没有 EXCEPTION / WARNING (除已知良性的)
- [ ] heartbeat / tailer 心跳正常 (`tailer alive · tick=...`)

### D. 自评汇报

- 完工汇报里写一句话**自我评估**: 「我修改了 X, 通过 A/B/C 自我审查, 唯一遗留是 Y」
- 不写 = 默认没做; 写了 = 上下文可以追溯

### E. 决策同步(只对大框架修改触发)

- [ ] 本次修改是否涉及**项目方向 / 大框架 / 否决某方案**?
  - **是** → 在 §9 「关键决策日志」加一条 (决策 + 为什么 + 反例 + 影响)
  - **否**(纯实施层: 改 regex / 修 bug / 调样式) → **不**加, 避免膨胀
- [ ] 加了决策, 自评汇报里点一句「同步了 §9 第 N 条」
- **触发判断**: ✅ 加新后端/前端 / 砍模块 / 换 TUI 策略 / 否决某尝试 ❌ regex 调优 / 加新命令 / 文案改

---

## 2. 关键架构事实 (实测, 不能错)

| 事实 | 来源 / 影响 |
|---|---|
| `cwd` 编码: **`/` 和 `.` 都替换为 `-`** | `~/.claude/projects/-home-pyadmin-claude-project-tmuxbot/` |
| `jsonl` 文件名 = `session_id`; `/clear`/`/compact` 创建新 jsonl 而非清空 | tailer 用 mtime 最新文件 |
| **claude TUI 事务式 flush jsonl**: tool_use + tool_result 必须配对才落盘 | AskUserQuestion picker 滞后,bot 端不可见 → 宪法封禁 |
| 9 种 jsonl `type`: `user`/`assistant`/`attachment`/`last-prompt`/`permission-mode`/`ai-title`/`system`/`file-history-snapshot`/`queue-operation` | 只推 user(过滤)/assistant/attachment |
| `paste-buffer -p` 后**必须 sleep 0.5s 再 Enter**, 否则 Enter 被当 paste 内容 | `tmux_send_text` 唯一安全时序 |
| `--resume` **不保留** `--dangerously-skip-permissions` (Issue #21974) | `ensure_claude` 每次都重传 |
| Telegram 4096 是 **UTF-16 单位** (中文 1 单位, HTML 标签算入) | `cwidth` / `split_for_tg` |
| Forum General topic `message_thread_id = None`, 回发**不能**传 thread_id | `source_key` 区分 topic vs 非 topic |
| Bot API 7.0+ `setMessageReaction` 支持 emoji 反应 (`👀` 已读) | `ack_received` middleware |
| `sendChatAction("typing")` 显示 ~5s, 每 4s 刷一次维持显示 | `heartbeat_typing_loop` |

---

## 3. 已知陷阱 (踩过的坑)

### A. asyncio.create_task GC 弱引用陷阱

Python 文档明确: event loop 只对 Task 弱引用, 不保存强引用 → 长时间 idle 时可能被 GC 中断。

**修复**: `State.bg_tasks` set 强引用所有 background task, `State.fire(coro)` 是统一入口。

### B. claude TUI busy 态下 paste + Enter 不可靠

claude 在 busy 时, tmux paste-buffer 内容进输入框 buffer, 但 Enter 不能 submit (race condition)。下一条消息进来时一起 submit。

**当前缓解**: 暂未修, 触发条件窄 (Boss 让先放着)。如修, 思路: paste 后 polling 验证 `tmux capture` 抓到内容, 才发 Enter。

### C. picker 检测假阳

正则匹配 "Enter to select" 等关键词 → 屏幕历史滚动里也可能出现 → 假阳无限循环推送。

**修复**: `PICKER_BOTTOMBAR_RE` 强制 3 个关键词必须**同一行**。

### D. /status 模型 regex 抓路径

`claude-[a-zA-Z0-9\-\[\]]+` 太宽松, `/tmp/claude-1000/` `claude-project/` 等路径里 `claude-XXX` 误匹配。

**修复**: `\b(?:claude-\d-\d|claude-[a-z]+-\d)[\w\-\[\]]*` 限定模型族名+版本号格式。

### E. capture_and_push 推 raw 屏幕太长

/compact 跑 120 轮没命中 done_pattern → 走 raw fallback 推 120 行屏幕到 TG。

**修复**: `CmdOpts.fallback_summary` 字段, /compact 配置简短文案, 不推 raw。

---

## 4. 项目特有红线

- ❌ **不能** root/sudo 跑 claude (Issue #9184 拒启动)
- ❌ **不要**项目里配 `PreToolUse` hook (Issue #37745 重置 bypass)
- ❌ **`tmux_send_text` 不前置 Escape**: Boss 发消息时不应中断 claude 当前生成 (要主动断 → `/esc` 或 `/cc`)
- ❌ **不主动 commit / push**: Boss 工作流是「文档先 → 代码同步」, commit 必须 Boss 明确发起
- ✅ `~/.claude/settings.json` 必含 `"skipDangerousModePermissionPrompt": true`
- ✅ TG bot 在群里**设为管理员**或 BotFather 关 privacy mode → 才能收非 @bot 消息
- ✅ ACL: `from_user.id == BOSS_USER_ID` (单一来源, 不要求 @bot, 任何 chat type)
- ✅ `.gitignore` 必含 `.env` / `bindings.yaml` / `data/`
- ✅ 改代码后必走 §1 自我审查清单

---

## 5. 关键文件路径

| 路径 | 用途 |
|---|---|
| `/home/pyadmin/claude-project/tmuxbot/tmuxbot.py` | 主程序 (单文件, 2017 行) |
| `/home/pyadmin/claude-project/tmuxbot/.env` | TG_BOT_TOKEN / BOSS_USER_ID (gitignored) |
| `/home/pyadmin/claude-project/tmuxbot/bindings.yaml` | binding 表 (gitignored) |
| `/home/pyadmin/claude-project/tmuxbot/data/tmuxbot.log` | 主日志 (按 tee 追加) |
| `/home/pyadmin/claude-project/tmuxbot/data/tmuxbot.lock` | 单实例 flock |
| `/home/pyadmin/claude-project/tmuxbot/data/offsets.json` | jsonl 上次读到的 byte offset |
| `~/.claude/projects/<encoded-cwd>/*.jsonl` | claude 写的事件日志 (tailer 读) |
| `~/.claude/CLAUDE.md` | Boss 全局宪法 |

---

## 6. 运维要点

### 启动 / 重启

```bash
# 通过 detached tmux session (推荐)
tmux kill-session -t tmuxbot-runner 2>/dev/null
tmux new-session -d -s tmuxbot-runner -x 156 -y 40 \
  "python3 tmuxbot.py 2>&1 | tee -a data/tmuxbot.log"

# 验证启动
grep "starting\|heartbeat\|polling" data/tmuxbot.log | tail -5
```

### 强杀残留

```bash
pkill -KILL -f "python3 tmuxbot.py" 2>/dev/null
rm -f data/tmuxbot.lock     # 清 stale flock 文件
tmux kill-session -t tmuxbot-runner 2>/dev/null
```

> ⚠️ 注意: aiogram 收 SIGTERM 后 polling 停, **但 jsonl_poll_loop 不会 cancel** → 进程僵尸。pkill 时**用 -KILL 不要 -TERM**。

### 跑通验证清单

1. `tail data/tmuxbot.log | grep heartbeat` 有 "heartbeat typing loop started" 行
2. 跑 `pgrep -af "python3 tmuxbot.py"` 有进程
3. Boss 在 DM 发 `/whoami`, bot 回 user_id / chat_id
4. Boss 在 group topic 发任意话, bot 注入 + claude 收到 + 回推到同 topic
5. `S.last_active[binding]` 更新 → typing 在干活时显示

---

## 7. Boss 工作流偏好 (项目特有)

- **远程协作**: Boss 通过 TG bot 自己跟自己的 claude 远程对话, 所以 picker 弹窗 (`AskUserQuestion`) **被全局宪法封禁** —— picker 数据 claude TUI 事务式 buffer, bot 端拿不到。**用纯文本 `1./2./3.` 选项 + Boss 回数字**代替。
- **任务面板 footer**: TG bot 端看不到 TUI tasks 区, **每条 assistant 消息末尾追加任务进度面板** (`━━━ 任务 ━━━` 块, 全局宪法要求)。
- **不要在 reply 里说「✓ 已完成」/「✅ 修复」类成就语**: Boss 看 diff 自己能判断, 重复说浪费屏幕。
- **stop 命令 (`/esc` `/cc` `/eof`) 是唯一中断手段**: 普通消息**不**前置 Escape, 不会打断 claude 当前生成。
- **远程 server `hbhy`**: 全局宪法记了 ssh, 项目暂未用上, 但部署到 server 时这是目标。

---

## 8. 路线图

| Milestone | 范围 | 状态 |
|---|---|---|
| **M1** | 单文件骨架 + 双 binding + 命令组 + heartbeat | ✅ 完成 (2026-05-27) |
| **M2** | 地毯代码审查 + 可插拔重构 (前端/后端解耦) | 🚧 进行中 |
| **M3** | 接入 codex cli (`codex-*` tmux session) | ⏳ 待开 |
| **M4** | 接入飞书前端 | ⏳ 远期 |

M2 重构方向 (Boss 决):

```
tmuxbot/
├── core/         # 主框架 (Binding/State/dispatcher/middleware)
├── backends/     # claude_code/, codex/, ... (TUI 注入 + jsonl 解析)
├── frontends/   # telegram/, feishu/, ... (消息收发 + 命令)
└── tmuxbot.py    # entry, 装配 backend + frontend
```

---

## 9. 关键决策日志 (给 AI 看的反漂移锚点)

> 这里记**项目方向 / 大框架 / 已否决方案**的「为什么」。
> 实施细节 grep 代码即可,不进这里。
> 改决策必先看这里; 加新决策必经 §1.E 触发判断。
> **时间逆序**,最新在最前。

---

### 2026-05-27 — 多 bot token 共存,1 bot ↔ 1 backend ↔ N tmux 子线程

- **决策**: 不同 CLI 类型(claude_code / codex / 未来其他)用**独立 bot token + 独立 TelegramFrontend 实例**,bindings.yaml 每个 binding 配 `bot_token_env` 字段路由
- **为什么**: 单 bot 同时接多种 CLI → 用户体验混乱(/status 之类命令在 claude 和 codex 行为不同)、命令菜单冲突、 状态机交叉污染;独立 bot token 让每个 bot 跟一种 CLI 一对一,清晰
- **架构原则(写进代码 `TOKEN_TO_BACKEND` 字典)**:
  - `TG_BOT_TOKEN` ↔ `claude_code` backend
  - `TG_CODEX_BOT_TOKEN` ↔ `codex` backend (独立 bot)
  - 启动时校验 binding.backend 跟其 bot_token_env 推断一致, 不一致强制对齐 + WARNING
- **反例已驳**:
  - 单 bot 多 backend 用 `/switch` 命令动态切换 → 用户视角:同一对话框忽 claude 忽 codex,体验混乱
  - 给 binding 加 backend 字段不加 bot 字段 → 一个 chat 不能既接 claude 又接 codex (find_by_source 单射)
- **影响**: `TelegramFrontend` 持有单个 backend + 自有 bindings 子集(不再用 backends dict);`__main__.py` 按 token 分组装配 N 个 frontend,并发 polling;heartbeat / tailer 每 frontend 一份

### 2026-05-27 — P3 可插拔重构落地 (单文件 → package)

- **决策**: `tmuxbot.py` 1911 行单文件 → `tmuxbot/` package, 拆 7 个 core 模块 + `backends/` + `frontends/` 两个子包
- **为什么**: ① 接 codex (M3) 需要 backend 抽象, 否则改一遍 parse_event/find_jsonl/ensure_running 等都得 fork;② 接飞书 (M4) 需要 frontend 抽象, TG handler 跟 IM 协议绑死会跟着 fork 一次
- **抽象接口**:
  - `Backend`: `parse_event(line) -> [(kind, body)]` / `find_active_jsonl(b)` / `find_tui_activity_fp(pane)` / `ensure_running(b)` / `command_opts()` / `command_aliases()` / `aggregate_usage(jsonl)`
  - `Frontend`: `send_html` / `edit_html` / `send_pre` / `send_chat_action` / `start_polling` / `stop` (+ TG 特有的 `send_picker_card`)
- **反例已驳**: 不拆继续单文件 → 接 codex 时 parse_event 要 if/else 两套 schema, 测试矩阵爆炸
- **影响**: `binding.yaml` 加 `backend: claude_code` 字段; root `tmuxbot.py` 变 thin entry (16 行); 旧文件备份 `tmuxbot.py.p2.5.bak`

### 2026-05-27 — 工具调用消息聚合到一条可编辑消息

- **决策**: assistant message 拆 `assistant_tools` (thinking + tool_use) 和 `assistant_text` (真说话) 两种事件。前者累计到一条 TG 消息用 `edit_message_text` 流式刷, 后者触发"封闭"再单独发
- **为什么**: 一条 jsonl assistant 可能含多个 tool_use, 串行触发多条 TG 通知 → Boss 收到刷屏。聚合到一条可更新消息符合 claude TUI 自身的折叠流式体验
- **实现要点**: `parse_event` 返回 `list[(kind, body)]` 而非单条; `State.tool_aggregator[binding] = {msg_id, content, last_ts}`; 静默 30s 或累计 > 3500 字符自动封闭开新
- **反例**: aggregator 用 reply 链 → TG 没有"原地刷新"的 reply 概念, edit 是正解
- **影响**: 所有 backend.parse_event 必须按这个 schema 返回事件列表

### 2026-05-27 — typing 心跳基于 TUI 状态行指纹, 而非 jsonl size

- **决策**: heartbeat 每 4s 抓 `tmux capture-pane`, 提取含「时间 + token」的状态行做指纹, 指纹变了 → 活跃 → 发 sendChatAction(typing)
- **为什么**: jsonl 写盘有 thinking 期间空窗 (claude 思考时 jsonl 不涨); TUI 显示的 `Xm Ys · ↓ Xk tokens` 这一行**实时刷**, 跟 Boss 看 TUI 体验完全一致
- **反例已尝试**: 早期版本基于 jsonl size 涨判活跃 → claude 长 thinking 时误判 idle, typing 闪烁
- **影响**: `heartbeat_typing_loop` 走 `tmux_capture` + `find_tui_activity_fp` 而非 jsonl tailer; `S.tui_fp` 字段保留指纹

### 2026-05-27 — 接入 codex cli 走可插拔后端, 而非 fork 项目

- **决策**: M2 重构为 `backends/{claude_code,codex}` + `frontends/{telegram,feishu...}` + `core/`, 单一 bot 进程多后端共存
- **为什么**: bot 主体能力 (tmux 注入 / jsonl tail / TG 收发 / picker / heartbeat) 跟后端模型无关; fork 多后端维护成本指数级 (改 ACL 要改 N 份, 改命令要改 N 份)
- **反例已驳**: 直接 fork 跑两个 bot → 后续接飞书前端时要 fork² 次; 命令/middleware/ack 都要改两遍, 出 bug 时机指数倍
- **影响**: M2 不能再单文件展开; backends 接口必须抽象 (`inject_text` / `parse_jsonl_event` / `tui_activity_fp` 等); frontends 同理 (`send_html` / `send_reaction` / `chat_action`)

### 2026-05-27 — AskUserQuestion / picker 工具全局封禁, 用 TG 纯文本数字选项替代

- **决策**: 任何"让用户选"的场景, 改为 bot 端发"`1. xxx / 2. yyy / 3. zzz` 回数字" 纯文本; AI 子 agent 也禁用 AskUserQuestion 工具
- **为什么**: claude TUI **事务式 flush jsonl** — picker 这种 tool_use 没配对 tool_result 时**不写盘**, bot 端拿不到 picker → Boss 远程在 TG 看不到选项 → 协作阻塞
- **反例已尝试**: ① picker 屏幕 OCR → ANSI 装饰 + 中文宽度难; ② picker 屏幕 regex parse → 选项布局多变; ③ picker tailer 等 buffer flush → 等到 Boss 主动发新消息才 flush, 滞后 25-75s 不可用
- **影响**: 全局 CLAUDE.md §6 + 项目 CLAUDE.md §4 + 兜底 `detect_idle_picker` 推屏幕原文 + 1-9 inline keyboard

---
