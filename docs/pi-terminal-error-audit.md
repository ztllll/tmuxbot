# Pi 终端不可继续错误巡检设计

## 目标与边界

为 **Pi route** 增加每 10 分钟一次、只读、精确 pane 的健康巡检：正常、空闲、工作中、重试/压缩中均静默；只有能够证明 Pi 已遇到明确错误、自动恢复已经结束、且没有后续可继续工作时，才向该 route 的原 IM endpoint 发送一次“疑似需要人工处理”通知。

本设计刻意不做以下事情：

- 不根据“长时间没有输出”猜测卡死；长推理、长工具调用、网络慢和 provider 长重试都可能合法。
- 不把 `Working...`、历史 scrollback 中的 `Error:`、工具自身执行失败、用户取消或 tmux 已手动关闭视为 Pi 终端错误。
- 不自动发送 `/esc`、`/restart`、`/new`，不重建已被人工关闭的 tmux session，不改变 provider 的重试策略。
- 不对 Claude Code 或 Codex route 生效。

这是“高置信度告警”，不是 watchdog 式的超时猜测器；宁可漏报无法证明的停滞，也不能把 Pi 的自动恢复过程吵到 IM。

## 已有基础与调研结论

### 当前 tmuxbot

- `PiBackend.parse_event()` 已将 transcript 中 assistant 的 `stopReason: "error"` 解析为 `PROVIDER_ERROR`，当前 reducer 会立即作为工具进度回推。这不足以判断故障是否最终不可恢复：Pi 可能已经把该错误写入 JSONL，随后仍在自动重试。
- `PiBackend` 只把带 braille spinner 的实时 `Working...` 识别为工作中，避免把历史文本误判为 busy；此约束必须保留。
- `lifecycle_watch_loop()` 现有巡检只处理精确 pane 的进程树不安全（例如停止态 Pi sibling），并会调用 `recover_unhealthy_pane()` 精确 `respawn-pane`。该恢复路径与本设计分离：它成功恢复时不应发人工错误通知。
- `tmuxbot-session-handoff.ts` 已经以 `tmux target + cwd + sessionId + transcriptPath` 写入原子、私有的 provider-authored record；健康状态应复用同样的精确身份验证原则，不允许按 mtime 猜 session。

### Pi 0.84.1 的事实

本机运行 Pi `0.84.1`。其官方 extension 文档明确说明：

- `agent_end` 只表示一次低层 run 完毕；Pi 之后仍可能自动重试、自动压缩并重试或处理已排队消息。
- `agent_settled` 才表示“没有 retry / compaction / follow-up 留下”，此时 `ctx.isIdle()` 应为真。
- `message_end` 会提供已完成 assistant message；assistant 的 `stopReason: "error"` 和 `errorMessage` 是结构化 provider 失败信号。
- `/new`、`/resume`、`/fork` 会销毁旧 extension runtime、重新装载 extension，并再次发出 `session_start`，因此状态必须 session-scoped 并在 session_start 重新声明。

当前本机 Pi 设置为 agent retry 5 次、基础延迟 20 秒，退避最多会持续约 10 分 20 秒（20/40/80/160/320 秒）。因此在 `agent_end` 或单条 JSONL error 时告警会稳定地产生假阳；必须等待 `agent_settled`。

Pi 上游也记录过“请求异常后 Working 无限显示”的历史缺陷：

- issue #2383（已关闭）指出旧版本错误未持久化、自动压缩后停滞；
- issue #4257（已关闭）指出 0.72.1 的 WebSocket transport error 停止 agent loop，0.73 已修复。

这说明结构化 error + settled 是正确的主信号；“spinner 很久不动”不是可靠证据。对于没有 error record 的静默卡死，本设计不告警，而是保留日志/人工 `/screen` 诊断入口。

## 推荐架构

新增一个深模块 `tmuxbot/runtime/pi_terminal_health.py`，对 caller 只暴露一个接口：

```python
async def pi_terminal_health_audit(frontends, state, paths, *, interval: float = 600) -> None
```

模块内部封装 record 读取、身份验证、状态分类、去重持久化和通知；`__main__.py` 只负责在包含 Pi binding 时用 `State.fire()` 启动它。不要把 regex、record 文件路径或去重细节散落进 lifecycle、heartbeat、frontend。

### 1. Pi provider-authored 状态 sidecar

将受管 extension 演进为一个 session handoff + health reporter；可继续沿用当前文件名，避免给存量 Pi 增加第二个需要手工启用的 extension。

在 `$TMUXBOT_STATE_DIR/pi-terminal-health/<ascii-slug>-<sha256(target)[:16]>.json` 原子写入，目录 `0700`、文件 `0600`：

```json
{
  "version": 1,
  "tmuxTarget": "SESSION:WINDOW.PANE",
  "cwd": "/absolute/project",
  "sessionId": "pi-session-id",
  "transcriptPath": "/absolute/session.jsonl",
  "state": "idle | working | recovering | terminal_error",
  "observedAt": "2026-08-12T03:21:45.000Z",
  "error": {
    "message": "bounded error text",
    "responseId": "optional provider response id"
  }
}
```

事件状态机：

| Pi extension 事件 | sidecar 状态 | 说明 |
|---|---|---|
| `session_start` | `idle` | 重新声明精确新 session，清除旧 session 的错误。 |
| `agent_start` | `working` | 正在请求/运行工具。 |
| assistant `message_end`, `stopReason=error` | 内存候选 error；`recovering` | 绝不在这里告警；Pi 可能重试。 |
| 下一条成功 assistant `message_end` | 清除候选 | 重试成功。 |
| `agent_settled` 且最后 assistant 是 error | `terminal_error` | Pi 文档定义的无 retry/compaction/follow-up 剩余，满足“未自愈并停止”。 |
| `agent_settled` 且最后 assistant 非 error | `idle` | 正常完成。 |
| `session_shutdown` | 不写新错误 | 新 session 的 `session_start` 会接管。 |

`aborted`、工具返回 `isError`、错误字样出现在普通文本、extension reload 中的旧状态均不得产生 `terminal_error`。sidecar 只记录结构化 assistant provider error，error 文本限制为 500 UTF-8 字符，避免写入过大的或敏感的 provider payload。

### 2. Python 验证与分类

每个 Pi binding 每 600 秒执行以下顺序：

1. tmux session 缺失：静默跳过，尊重 `/tmuxstop` 与人工关闭。
2. pane 进程树不安全：交由既有 `lifecycle` 的精确恢复路径；恢复中或恢复成功均静默。健康巡检本身不重启任何 pane。
3. 读取 sidecar，拒绝 symlink、非绝对路径、错误 target/cwd/session、不可读 JSON、JSONL header 不匹配的记录。缺 record 也静默，避免扩展未加载时制造误报。
4. 当前 `provider_session_id/transcript_path`、handoff 和 sidecar 必须一致；session 切换期间或 identity 尚未收敛时静默。
5. `working` / `recovering` / `idle`：静默；可写 debug audit，不发 IM。
6. 仅 `terminal_error` 进入告警候选。再确认 Pi 进程树仍安全、最新 transcript 的最后 assistant 仍为同一个 `stopReason=error`，且之后没有 user/assistant 成功消息。任一不满足即丢弃候选。

第 6 步避免 sidecar 在 crash、reload 或下一次用户提交已经恢复后仍把旧错误当作当前故障。

### 3. 一次通知和持久去重

将已发送 fingerprint 原子保存为 `$TMUXBOT_STATE_DIR/pi-terminal-health-audit.json`：

```text
sha256(route name + sessionId + responseId/error hash + transcript path)
```

同一个 fingerprint 仅通知一次，bridge 重启不重复。发生 `/new`/`/resume`/`/fork`，或同一 session 出现新的 terminal error，fingerprint 改变后可重新通知。

通知发送到 binding 的精确 `(chat_id, thread_id)`，最多包含：route 名称、tmux target、Pi 已停止自动恢复的时间、截断并 HTML 转义的 error 摘要，以及 `/screen` 与人工查看 TUI 的建议。不得附带完整 transcript 或自动执行修复命令。

建议文案：

```text
⚠️ Pi 疑似已停止且未完成自动恢复
· route: <name> · target: <target>
· Pi 已结束 retry/compaction/follow-up，最后错误：<summary>
· 请用 /screen 查看该 TUI；确认后可人工 /restart 或在终端处理。
```

## 与现有 JSONL 回推的改动

为满足“只在明确不可自愈错误时通知”，Pi 的 `stopReason=error` 不能再直接作为普通 `PROVIDER_ERROR` 推送到 IM。它应被记录为 health sidecar 的 `recovering` 候选；只有之后侧车记录成为 `terminal_error`，审计模块才通知。

正常 assistant 文本、工具进度、压缩 lifecycle 和其他 backend 的 provider errors 保持原行为。此收敛仅适用于 `backend == "pi"`，并不会让 Pi 的最终错误永久消失：它将在最迟一个审计周期内以更准确、可操作且去重的形式送达。

## 配置、可观测性与失败模式

- `TMUXBOT_PI_TERMINAL_HEALTH_ENABLED=1`：默认启用；`0/false/no/off` 显式关闭。
- `TMUXBOT_PI_TERMINAL_HEALTH_INTERVAL=600`：默认 10 分钟；下限 60 秒，仅供受控测试。
- 每 tick 输出 debug 汇总：checked / ignored-working / ignored-recovering / invalid-record / terminal-error / notified。
- sidecar 无效、extension 未加载、tmux capture 失败、IM 发送失败只记日志，不把未知状态升级为用户错误。
- IM 发送失败时不写 fingerprint，下一次审计重试；发送成功后才持久化 fingerprint。

## 验收矩阵

1. 正常 idle Pi、正常 working Pi、长工具调用：0 通知。
2. 429/502/503 后 Pi retry 成功：extension 经过 `recovering → idle`，0 通知。
3. retry 穷尽后的 `agent_settled + stopReason=error`：10 分钟内对精确 endpoint 通知 1 次。
4. 同一错误跨 bridge restart：0 重复通知。
5. 下一条成功 assistant、`/new`、`/resume`、`/fork` 后旧 error：0 通知；新 session 重新建立身份。
6. 进程树含 stopped Pi sibling：既有精确 pane recover 成功后 0 通知；不会影响其他 pane。
7. 手工 `/tmuxstop` 或不存在的 tmux session：0 通知、0 重建。
8. 声称 error 但 target/cwd/session header 不匹配、路径为 symlink、sidecar 损坏：0 通知。
9. Pi extension 未加载或没有 sidecar：0 通知（fail closed）。
10. Pi 已知历史 silent stall（没有结构化 error）：0 自动告警，日志说明无法满足高置信度；人工 `/screen` 仍可诊断。

## 实施顺序

1. 为 sidecar 路径和 JSON validator 写纯 Python 测试，覆盖 Unicode tmux target、permissions、symlink、session/cwd/header 不匹配。
2. 为 TypeScript extension 的状态机写 isolated fake-API 测试，确认 `agent_settled` 前不会写 `terminal_error`。
3. 实现 Python classifier 和持久 fingerprint store，再写上述通知/去重测试。
4. 将 Pi `stopReason=error` 的即时 reducer 路由改为延后健康审计；回归 JSONL、Telegram、飞书和 lifecycle 测试。
5. 在非生产 Pi route 上演练一个可控的 retry-success 与 retry-exhausted transcript fixture；确认没有触发真实 provider 费用或破坏 TUI。
6. 部署后先观察一周 audit debug 指标，再考虑扩展错误类别；不得通过放宽“无进展超时”来提高告警数量。

## 来源

- Pi extension lifecycle 官方文档：`/home/pyadmin/.npm-global/lib/node_modules/@earendil-works/pi-coding-agent/docs/extensions.md`（本机随 Pi 0.84.1 安装；agent_end、agent_settled、message_end、session replacement 定义）。
- Pi 官方 settings 文档：<https://raw.githubusercontent.com/earendil-works/pi/main/packages/coding-agent/docs/settings.md>（retry.enabled、maxRetries、baseDelayMs、provider retry 语义）。
- Pi 已安装实现：`dist/core/agent-session.js`（agent_end 后 `_handlePostAgentRun()` 处理 retry/compaction，最后才 `agent_settled`）。
- Pi issue #2383：<https://github.com/earendil-works/pi/issues/2383>（旧版本异常+compaction silent stall，closed）。
- Pi issue #4257：<https://github.com/earendil-works/pi/issues/4257>（WebSocket transport 错误在 0.73 修复，closed）。
