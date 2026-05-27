# tmuxbot.py 地毯级代码审查

> 范围: `tmuxbot.py` 2017 行单文件, M2 重构前的现状评估。
> 输入: Explore agent 自动扫 + 我亲读全文件交叉。
> 输出: 问题清单 + 优先级 + 简短修复方向(不出代码), Boss 决定哪些必修后我再下手。

---

## 0. 概述

| 严重度 | 数量 | 含义 |
|---|---|---|
| 🔴 **必修** | 5 | 阻塞 event loop / async-sync 混用, 实际影响性能 |
| 🟡 **建议改** | 9 | 重复代码 / 命名不一 / 死代码 / 健壮性问题 |
| 🟢 **可选** | 6 | 风格 / magic number / 注释 |
| 🟦 **架构层** | 5 | M2 重构必须解决, 不是单点改能修的 |
| **总计** | **25 类问题** | (Explore agent 找了 39 条具体行号点,我归并去重得 25 类) |

**判断:** M1 单文件能跑通是奇迹, 但**距离可维护、可扩展还有距离**。M2 重构是对的方向。本审查重点标 🔴 和 🟦 — 重构时这些必处理。

---

## 1. 🔴 必修问题 (5 条)

### R1. `tmux_send_text` 阻塞 event loop 0.5 秒

- **位置**: `tmuxbot.py:213-224`
- **问题**: `tmux_send_text` 是 sync 函数, 内部 `time.sleep(SEND_KEYS_DELAY)` 阻塞主 event loop。每条 Boss 消息进来都卡 0.5s, 期间 polling/heartbeat/TG send 全冻结。
- **测量**: ack middleware 收到消息 → tmux_send_text → 0.5s 阻塞 → 所有 binding 的 tailer/heartbeat tick 推迟
- **修复方向**: 改 `async def tmux_send_text`, 用 `await asyncio.sleep` + `asyncio.create_subprocess_exec`

### R2. `auq_resolve` async 函数里 sync `time.sleep`

- **位置**: `tmuxbot.py:715-718`
- **问题**: AskUserQuestion 回调里 `time.sleep(0.05)` × N 模拟 ↓ 键间隔, 是 sync sleep, 阻塞 event loop
- **修复方向**: `await asyncio.sleep`

### R3. `on_picker_callback` 同样的 sync sleep

- **位置**: `tmuxbot.py:1891-1894`
- **问题**: 同 R2, picker 回调也用 sync sleep
- **修复方向**: 同 R2

### R4. `ensure_claude` 阻塞 2.5 秒

- **位置**: `tmuxbot.py:232-243`
- **问题**: sync `time.sleep(0.5)` + `time.sleep(2.0)`, 总共 2.5s 阻塞。每次 `on_text` 调用都触发(虽然多数情况 claude 已在跑会跳过 sleep, 但首次启动绝对阻塞)
- **修复方向**: `async def ensure_claude` + `await asyncio.sleep`

### R5. `save_offsets` 在 hot loop sync 写盘

- **位置**: `tmuxbot.py:562`(`jsonl_poll_loop` 每次发现新行都调)
- **问题**: claude 写一行 jsonl bot 就同步写一次 offsets.json, sync I/O 阻塞 event loop。token-by-token 流式响应时, 每 token 都 sync 写盘
- **修复方向**: batch 或定期 flush; 用 `aiofiles`

---

## 2. 🟡 建议改 (9 条)

### Y1. ACL + find_by_source 重复 5 次

- **位置**: `tmuxbot.py:1577-1582, 1684-1689, 1696-1702, 1744-1750, 1800-1808`
- **问题**: 同样 5 行 boilerplate 在 5 个 handler 重复, 改一处忘改其他处的风险
- **现状**: 已抽 `_send_key_to_binding` 用于 3 个简单命令, 其他没抽
- **修复方向**: 写一个 `@require_binding` decorator, 或 `async def get_binding_or_reply(m) -> Binding | None`

### Y2. 11 个裸 except 吞错误无 log

- **位置**: Explore agent 列的 11 处, 主要在 picker / parse / cb 路径
- **问题**: 真发生错时静默, 调试痛苦
- **修复方向**: 改成 `except Exception as e: log.debug(f"... err: {e}")` 至少留痕

### Y3. `render_ask_user_question` 多 question 丢数据

- **位置**: `tmuxbot.py:610`
- **问题**: `q = questions[0]` 直接取第一个; AskUserQuestion 上限 4 questions, 其他被丢
- **现实**: AskUserQuestion 已被宪法封禁, 这是死路径
- **修复方向**: 删掉 AUQ 整套代码, 或留一个 warning log

### Y4. `COMPACT_BUSY_RE` 死代码

- **位置**: `tmuxbot.py:1020`
- **问题**: 定义了 `COMPACT_BUSY_RE` 但从未使用
- **修复方向**: 删

### Y5. `save_binding_chat_id` regex 替换 YAML 不健壮

- **位置**: `tmuxbot.py:1375-1381`
- **问题**: 在 YAML 文本上做 regex 替换。若多行 chat_id 字段、注释紧贴、缩进异常 → 替换失败
- **修复方向**: `yaml.safe_load → 修改 dict → yaml.safe_dump`(虽然会丢注释, 但 bindings.yaml 注释不算关键)

### Y6. `_safe_on_event` wrapper 与 `S.fire` 已有的异常捕获重复

- **位置**: `tmuxbot.py:569-573` + `tmuxbot.py:113-119` (`S.fire`)
- **问题**: `S.fire` 已经用 `add_done_callback` 处理异常, `_safe_on_event` 再 wrap 一次, 双层 try
- **修复方向**: 删 `_safe_on_event`, 让 `S.fire` 的 done_callback 也 log exception(目前只 discard)

### Y7. 命名前缀混用 (`do_*` vs `cmd_*` vs `on_*`)

- **位置**: 全文
- **问题**: 同类函数命名不一致 — `do_setup` / `cmd_whoami` / `on_text` / `on_file` / `on_picker_callback`
- **修复方向**: 统一前缀(handler 都用 `on_*`, 业务函数都用 `cmd_*` 或别的)

### Y8. `capture_and_push` 121 行单函数

- **位置**: `tmuxbot.py:1197-1317`
- **问题**: 1 个函数管 polling + 早退 + render + cleanup, 职责过多
- **修复方向**: 拆 `poll_until_settled` + `render_summary` + `cleanup_modal`

### Y9. 字符串硬编码 magic number 多

- **位置**: 散落, e.g. 80(capture lines), 28(button label), 120/150/200(preview)
- **修复方向**: 集中常量, 或加注释解释为什么是这个值

---

## 3. 🟢 可选 (6 条)

### G1-G6 (打包说)

- G1. `parse_event` 命名 vs `format_tool_use` 不对称 — 一个 parse 一个 format
- G2. `strip_decorations` 不可逆 (设计选择, 但限制了 `/screen` 之类保留 ANSI 颜色的扩展)
- G3. `encode_cwd` 在 utils 区被 `Binding.jsonl_dir` 用, 形成 forward reference 轻微耦合
- G4. lambda 没 type hint(行 1713)
- G5. 缺 systemd / supervisor 脚本, 当前用 tmux session 包一层
- G6. 缺单元测试 — 重构时风险高

---

## 4. 🟦 架构层 (5 条, M2 重构必须解决)

### A1. backend 跟 bot 主体紧耦合

- **现状**: `tmux_send_text/key/capture`, `jsonl_poll_loop`, `parse_event`, `ensure_claude`, `find_active_jsonl` 全部写死 claude cli 的事实
- **影响**: 接 codex cli 时必须**全部重写**这些函数
- **重构方向**: 抽象 `Backend` 接口
  ```python
  class Backend(ABC):
      def inject_text(self, target, text): ...
      def inject_key(self, target, key): ...
      def capture(self, target, lines): ...
      def ensure_running(self, binding): ...
      async def tail_events(self, binding): yield (kind, body)
      def find_activity_fingerprint(self, pane): ...
  ```
  `ClaudeBackend` / `CodexBackend` 各实现

### A2. frontend 跟 TG 紧耦合

- **现状**: 所有命令 handler (`cmd_status`, `cmd_info`, `cmd_screen`, `on_text`, `on_file` 等)直接用 `aiogram.types.Message` / `cq.message.edit_text` / `bot.send_message`
- **影响**: 接飞书前端时必须重写整条 handler 链
- **重构方向**: 抽象 `Frontend` 接口 + `IncomingMessage` / `OutgoingMessage` 中间类型
  ```python
  class Frontend(ABC):
      async def send_html(self, target, text): ...
      async def send_pre(self, target, text): ...
      async def send_reaction(self, msg_ref, emoji): ...
      async def send_chat_action(self, target, action): ...
      async def start_polling(self): yield IncomingMessage
  ```
  `TelegramFrontend` / `FeishuFrontend` 各实现

### A3. State 单例全局可见, 测试 / 多实例不便

- **现状**: `S = State()` 模块级单例, 所有函数都用 `S.fire / S.bindings / S.bot`
- **影响**: 测试时无法 mock; 未来想跑多 bot 实例时硬冲突
- **重构方向**: `State` 用依赖注入传给各模块, 不要全局; `S.bot` 拆成 `frontend.client`

### A4. `source_key` 跟 Telegram Message 结构耦合

- **位置**: `tmuxbot.py:1388-1393`
- **问题**: `m.chat.type == "private"` / `m.is_topic_message` 是 TG-specific
- **影响**: 飞书前端时 source 形态不同
- **重构方向**: frontend 各自负责 source_key, 主框架只关心抽象 `BindingKey`

### A5. 命令组跟 backend 行为绑死

- **位置**: 所有 `parse_*` (parse_context / parse_cost / parse_status / parse_compact) — 写死 claude TUI 的输出格式
- **影响**: codex 后端的 `/status` 输出格式不同, 这些 parser 全失效
- **重构方向**: parser 归 backend 私有, 主框架只调 `backend.parse(cmd, raw) -> str | None`

---

## 5. 总结 + 建议路径

### 推荐顺序

**Step 1 — 必修 R1-R5 (event loop 阻塞)**

这是性能问题, 影响实际使用。建议在 M2 重构开始**之前**先修, 避免重构时把阻塞调用原样搬到新模块。

**Step 2 — 删死代码 Y3, Y4 (AUQ 路径, COMPACT_BUSY_RE)**

减少重构面积。AUQ 整套(`render_ask_user_question` + `auq_resolve` + `on_auq_callback`)~150 行可以删, picker 兜底已经够用。

**Step 3 — 收 Y1, Y2, Y6 (ACL/except 重复, _safe_on_event)**

抽 decorator + 加 log + 删冗余 wrapper, 减少噪音。约 -100 行。

**Step 4 — 进入 M2 架构重构 (A1-A5)**

按 `core/` + `backends/` + `frontends/` 拆。详见 §6 提议结构。

**Step 5 — Y5, Y7, Y8, Y9 + G1-G6**

重构后顺手清理。

### 数字估算

- 当前: 2017 行
- 删 AUQ + 死代码: -200 行 → 1817 行
- 修 R1-R5: ±0 行 (改 sync 为 async)
- 重构成 `core/` + `backends/` + `frontends/`: 单文件解体, 每模块 ~200-400 行
- 接入 codex backend: +1 模块 ~300 行

### 我的判断

- 🔴 必修 5 条 — **强烈建议在 M2 重构前修**, 否则把烂代码原样搬迁
- 🟡 + 🟢 — 重构同步收
- 🟦 架构层 5 条 — **这是 M2 重构的真正内容**, 已经超出"审查"范畴, 进入"设计"

---

## 6. 提议的 M2 结构

```
tmuxbot/
├── pyproject.toml
├── README.md
├── CLAUDE.md
├── DEVELOPMENT.md
├── CODE_REVIEW.md           ← 本文件
├── bindings.yaml
├── .env
├── data/
└── tmuxbot/                 ← Python package
    ├── __init__.py
    ├── __main__.py          ← 入口 entry, 装配 backend + frontend
    ├── core/
    │   ├── state.py         ← State, Binding
    │   ├── dispatch.py      ← 中间路由, ACL, find_by_source
    │   ├── pending.py       ← pending_rename / pending_questions 状态机
    │   └── utils.py         ← cwidth / cpad / render_table / utf16_len / split_for_tg
    ├── backends/
    │   ├── base.py          ← Backend 抽象基类
    │   ├── claude_code.py   ← 当前 tmuxbot.py 的 claude 逻辑迁过来
    │   └── codex.py         ← M3 新加
    ├── frontends/
    │   ├── base.py          ← Frontend 抽象基类 + IncomingMessage/OutgoingMessage
    │   ├── telegram.py      ← 现有 aiogram 逻辑搬过来
    │   └── feishu.py        ← M4 新加 (用 lark-oapi)
    └── commands/
        ├── base.py          ← 命令注册接口
        ├── status.py        ← /status (走 backend 的 parser)
        ├── info.py          ← /info
        ├── session.py       ← /new /resume /rename
        └── control.py       ← /esc /cc /eof /screen /restart
```

入口装配:

```python
# tmuxbot/__main__.py
async def main():
    state = State.from_config()
    backend = ClaudeBackend()        # 或 CodexBackend(), 从 binding 决定
    frontend = TelegramFrontend(token)
    dispatcher = Dispatcher(state, backend, frontend)
    register_commands(dispatcher)
    await dispatcher.run()
```

---

## 7. 给 Boss 的决定项

请回数字, 多选可:

1. **现在按 Step 1-5 顺序做** (推荐, 渐进, 每一步可单独验证)
2. **跳过 Step 1-3, 直接进 M2 重构** — 重构时顺手修(风险:重构 + 调试同时, 容易卡 bug)
3. **只修 🔴 必修 5 条, M2/M3 暂缓** — 先求稳, 重构延后
4. **审查报告记一下, 当前 M1 跑稳就行, 不重构** — 不接 codex
5. **有补充审查方向要看**(告诉我哪里再细化)

我等 Boss 决定。
