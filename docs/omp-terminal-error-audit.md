# OMP 终端错误与疑似失活巡检设计

## 目标与边界

为 **OMP route** 提供两层、只读、精确 pane 的健康巡检：

1. provider-authored `terminal_error`：受管 OMP extension 已证明结构化 provider error 没有继续执行；
2. suspected stall：没有结构化终态错误，但真实 OMP TUI 长期保持工作态，且屏幕与 JSONL 均无进展。

两层都只向该 route 的原 IM endpoint 发送一次人工检查通知。巡检不修改 tmux、OMP、route 或 transcript，不自动执行 `/restart`、`/new`、`/esc`，也不复活被人工关闭的 tmux session。

本设计刻意不把以下情况直接判为终端错误：

- 单次 assistant `stopReason: "error"`；OMP 后续仍可能 retry、compact 或处理 follow-up；
- `working` / `recovering` sidecar、活动工具子进程或待完成的 session handoff；
- 工具结果 `isError`、普通文本中的 `Error:`、历史 scrollback 或仅仅“很久没有新消息”；
- 已知用户取消、手工 `/tmuxstop`、缺失 tmux session、无效或缺失 sidecar；
- Claude Code 或 Codex route。

第一层追求可证明的 provider 终态；第二层只报告“疑似失活”，不能把它描述成已确认 provider error。

## 当前 OMP 契约

目标运行时是 Oh My Pi `omp/17.3.2`。tmuxbot 不使用 RPC、SDK 或 headless mode；每次受管启动都由 provider registry 生成：

```text
omp --approval-mode yolo --extension <tmuxbot-session-handoff.ts 的绝对路径>
```

有持久化 session pin 时只追加：

```text
--resume <绝对 JSONL 路径>
```

受管扩展导入 OMP 当前发布的 `@oh-my-pi/pi-coding-agent` extension API。上游包名中的 `pi-coding-agent` 是依赖名，不是 route/backend、binary、环境变量或兼容 alias。

OMP JSONL 通常位于 `~/.omp/agent/sessions/<project-key>/<timestamp>_<id>.jsonl`。tmuxbot 不扫描该目录挑最新文件；当前会话由 provider-authored handoff 与 binding 的精确 pin 决定。

## Provider-authored identity 与 health sidecar

同一个受管扩展原子写入两类 version 1 record，目录 `0700`、文件 `0600`：

- `$TMUXBOT_STATE_DIR/omp-session-handoffs/<target-key>.json`
- `$TMUXBOT_STATE_DIR/omp-session-health/<target-key>.json`

handoff 包含：

```json
{
  "version": 1,
  "tmuxTarget": "SESSION:WINDOW.PANE",
  "cwd": "/absolute/project",
  "sessionId": "omp-session-id",
  "transcriptPath": "/absolute/session.jsonl",
  "processId": 12345
}
```

health 复用完全相同的身份并增加状态：

```json
{
  "version": 1,
  "tmuxTarget": "SESSION:WINDOW.PANE",
  "cwd": "/absolute/project",
  "sessionId": "omp-session-id",
  "transcriptPath": "/absolute/session.jsonl",
  "state": "idle | working | recovering | terminal_error",
  "observedAt": "2026-08-14T03:21:45.000Z",
  "error": {
    "message": "最多 500 字符的错误摘要",
    "responseId": "可选 provider response id"
  }
}
```

`error` 只在 `terminal_error` 中持久化。record 名由 tmux target 的 ASCII-safe slug 与 target SHA-256 前 16 位组成，避免不同 pane 互相认领。

OMP 新 session 的 transcript 可能在首条消息前尚未创建。handoff 对这种 pending identity 额外要求：路径位于官方 `~/.omp/agent/sessions` root、文件名后缀匹配 `sessionId`，且 `processId` 是 exact pane 中仍存活的 OMP process。文件一旦出现，所有消费路径立即恢复 JSONL header/cwd/session ID 校验。health sidecar 不依赖 `processId`，且 terminal-error 审计只消费已经落盘并通过 header 校验的 transcript。

## OMP extension 事件状态机

当前 OMP event surface 没有本设计可依赖的 `agent_settled`。终态必须由 `agent_end` 的 `isTerminal` / `willContinue` 判断，不能沿用旧 runtime 的 settled 约定。

| OMP extension 事件 | 内存/sidecar 变化 | 判定 |
|---|---|---|
| `session_start` | `refreshIdentity(ctx)`；清 error candidate；handoff + health=`idle` | 从 `ctx.sessionManager.getSessionFile()` / `getSessionId()` 读取当前身份。 |
| `session_switch` | 与 `session_start` 完全相同 | 覆盖 `/new`、`/resume`、`/fork`、`/import` 等原生会话切换。 |
| `agent_start` | 清旧 candidate；health=`working` | 新一轮请求/工具执行开始。 |
| assistant `message_end`, 非 error | 清 candidate | 成功消息使之前的候选失效；最终 `idle/working` 由后续 `agent_end` 写入。 |
| assistant `message_end`, `stopReason=error`，且为已知用户取消 | 清 candidate，不写 `recovering` | 用户主动中止不是 provider terminal error。 |
| assistant `message_end`, `stopReason=error`，其他错误 | 保存截断 error candidate；health=`recovering` | 这里只是候选，绝不告警。 |
| `agent_end` 且 `isTerminal === false` 或 `willContinue === true` | 有 candidate → `recovering`；无 candidate → `working` | OMP 明确表示 retry/compaction/follow-up 仍会继续。 |
| 其他 terminal `agent_end` | 有 candidate → `terminal_error`；无 candidate → `idle` | 只有这里才能把结构化候选升级为终端错误。 |

已知用户取消使用窄白名单匹配，忽略大小写和句末标点：`operation aborted`、`request was aborted`、`the operation was aborted`、`this operation was aborted`。不要把所有包含 `abort` 的 provider error 都静默。

`message_end` 中的 error 文本最多保留 500 个字符；工具失败、普通 assistant 文本、extension reload 遗留状态都不能生成 `terminal_error`。

## 第一层：已确认 terminal error

`audit_omp_terminals_once()` 每个周期按以下顺序处理 OMP binding：

1. tmux session 不存在：清除此 route 的疑似失活采样并静默，尊重人工关闭。
2. 读取 exact-target health sidecar。缺失、symlink、不可读、JSON/version/state 不合法、非绝对 cwd/transcript 均 fail closed。
3. sidecar 的 `tmuxTarget` 与 canonical route cwd 必须精确匹配；JSONL header 必须支持 version 1–3，且首个有效 `type:"session"` 的 `id/cwd` 与 sidecar 一致。
4. health 的 `sessionId/transcriptPath` 必须同时等于 binding 的 `provider_session_id/transcript_path`，并与当前 exact-target handoff 一致；会话切换尚未收敛时静默。
5. 只接受 `state="terminal_error"`、非用户取消且 OMP process tree 仍安全的 record。
6. 反向检查 transcript 最后的相关 message：它仍须是同一条 assistant `stopReason="error"` 和同一截断 `errorMessage`；之后出现 user message 或成功 assistant message都会使旧 sidecar 失效。
7. 全部成立才向 binding 的精确 `(chat_id, thread_id)` 发送一次通知；不等待 suspected-stall 采样。

通知不附完整 transcript，也不声称 tmuxbot 已修复故障：

```text
❌ OMP 已停止自动恢复，需要人工处理
· route: <name> · target: <target>
· 最后错误：<bounded summary>
· OMP 已结束 retry、compaction 和 follow-up；请用 /screen 查看 TUI。
```

terminal error fingerprint 为：

```text
sha256("terminal-error" + sessionId + responseId/errorMessage + transcriptPath)
```

同一 fingerprint 只通知一次；bridge 重启后仍去重。新 session 或新的 response/error 会生成新 fingerprint。IM 发送失败不记录成功状态，下个周期重试。

## 第二层：只读 suspected-stall

第二层保留对“没有结构化 terminal error 的静默失活”的保守观察。它不依赖错误字样，也不会改变 provider 状态。

进入采样前必须同时满足：

- route backend 是 `omp`，精确 tmux session 与 pane 仍存在；
- 没有 pending session handoff，没有已知 compaction；
- OMP process tree 安全，且没有活动的非 shell 子进程；
- 严格 OMP footer/parser 识别当前 live region 为 `WORKING`，label 不是 `retrying`；
- `find_active_jsonl()` 返回 handoff 或 binding pin 指向的 exact transcript；binding session id/path 与 `session_identity()` 一致。

任一条件不满足都清除该 route 的连续采样并静默。尤其不能因为 sidecar 缺失、capture 失败或身份未知而升级成失活。

进展 fingerprint 只使用：

```text
sha256(transcript 文件大小 + 规范化后的当前 pane capture)
```

规范化会消除 braille spinner 帧和 footer 时钟变化；这些动画只证明 renderer 活着，不证明 provider request 有进展。transcript mtime 也不算 durable progress，只有文件大小和可见内容变化会重置采样。

默认 interval 为 600 秒：第一次观察建立 baseline，之后连续 3 次 fingerprint 不变才通知，因此最早约在可观察进展停止 30 分钟后报告。通知必须明确是“疑似失活”，并建议 `/screen` + SSH 人工查看；同一 fingerprint 只通知一次，发生进展或 session 变化后才可建立新的采样序列。

`recovering` sidecar 本身不会触发第一层通知；如果 OMP 长期仍显示严格工作态且满足第二层全部条件，第二层最多只能报告 suspected stall，不能把它升级描述为 terminal error。

## 持久状态、配置与日志

通知去重和 suspected-stall 采样原子保存到：

```text
$TMUXBOT_STATE_DIR/omp-terminal-health.json
```

可配置项：

```dotenv
TMUXBOT_OMP_TERMINAL_HEALTH_ENABLED=1
TMUXBOT_OMP_TERMINAL_HEALTH_INTERVAL=600
TMUXBOT_OMP_TERMINAL_HEALTH_FILE=/home/you/.local/state/tmuxbot/omp-terminal-health.json
```

- `ENABLED` 默认开启；`0/false/no/off` 关闭。
- `INTERVAL` 默认 600 秒，下限 60 秒。
- `FILE` 覆盖去重/采样 registry；不改变 provider-authored sidecar 目录。
- registry 目录/文件权限分别为 `0700/0600`，symlink 或损坏内容按空 registry 处理。
- 每个 tick 记录 `checked/silent/notified` debug 汇总；未知状态只记日志，不发 IM。

服务启动日志：

```text
OMP terminal-health audit starting · interval=600.0s
```

## 操作者处理方式

收到任一通知后先用 `/screen` 查看精确 pane，再 SSH attach 处理 OMP 原生界面。菜单、picker、ask、approval、plan review 与确认全部是 SSH-only；IM 不发送方向键、Enter、Escape、批准或取消。bot `/plan` 只是本地帮助，不会向 OMP 注入不存在的 `/plan` 命令。

如果需要 `/restart`，OMP 采用 clean pane respawn，不向原生 TUI 注入 Ctrl-C/Ctrl-D；随后仍用 registry 固定参数启动。有 transcript pin 时只通过 `--resume <绝对路径>` 精确恢复。pin/header/cwd 无效、OMP resume 失败或新进程未发布匹配 handoff 时必须保留 pin并显式失败，禁止静默开启替代会话。

若操作者在真实 TUI 外带切换 session 且 route 没有跟随，使用 `tmuxbot admin adopt-omp-session ROUTE --session-file <绝对 JSONL>` 的 plan/`--apply` 流程；不得按 mtime 猜测。

## 验收矩阵

1. 正常 idle、working、长工具调用：0 terminal-error 通知；活动 workload 不累计 suspected-stall。
2. provider error 后 `agent_end isTerminal=false` 或 `willContinue=true`：`recovering`，0 terminal-error 通知。
3. retry 成功并出现非 error assistant message：清 candidate；terminal `agent_end` 后 `idle`，0 通知。
4. 已知用户取消：不写 `recovering/terminal_error`，0 通知。
5. error candidate 后 terminal `agent_end`：sidecar=`terminal_error`；下个 audit 对精确 endpoint 通知 1 次。
6. 同一 terminal error 跨 bridge restart：0 重复通知；新 response/session 可通知一次。
7. terminal_error 后又出现 user 或成功 assistant message：旧 record 失效，0 通知。
8. `/new`、`/resume`、`/fork`、`/import`：`session_switch` 刷新 handoff + idle health；旧 session error 不泄漏。
9. target/cwd/session/header/path 不匹配、sidecar symlink/损坏、extension 未加载：fail closed，0 通知。
10. tmux session 被人工关闭：0 通知、0 重建。
11. 屏幕或 transcript 有任何 durable progress：suspected-stall 计数归零。
12. 严格 WORKING + exact identity + 无 workload/retry/compaction/handoff，baseline 后连续 3 个十分钟 fingerprint 不变：只通知 1 次“疑似失活”。
13. `/restart` 使用 clean respawn 和 exact `--resume`；恢复失败保留 pin，不静默新建 session。

## 实现锚点

- `omp-extensions/tmuxbot-session-handoff.ts`：identity refresh 与 extension health 状态机。
- `tmuxbot/runtime/omp_handoff.py`：handoff、OMP JSONL header 与 exact identity 校验。
- `tmuxbot/runtime/omp_session_health.py`：health sidecar validator。
- `tmuxbot/runtime/omp_terminal_health.py`：terminal-error 通知、suspected-stall 采样与持久去重。
- `tmuxbot/providers/adapters.py`：server-owned OMP executable metadata 与固定 launch argv。
- `tmuxbot/backends/omp.py`：exact resume、managed handoff fail-closed 与 OMP TUI 弱信号解析。
