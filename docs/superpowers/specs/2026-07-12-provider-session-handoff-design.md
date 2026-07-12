# Provider 会话切换设计

## 目标

当 Telegram 或飞书向某个 tmux binding 发送 `/new` 或 `/clear` 时，bot 必须将该 binding 从旧 provider transcript 切换到本次命令新建的 transcript；切换后的身份必须持久化，并在 CLI/bridge 重启时恢复新会话。

## 根因

`3d4dcbb` 为避免同目录多 binding 串消息，将已保存的 transcript 设成 `find_active_jsonl()` 的绝对优先项。于是新 transcript 永远不会被选中，jsonl tailer 无法更新 provider session identity，后续 `resume` 也始终使用旧 ID。

## 方案

为 Binding 增加仅运行期的 `pending_session_handoff_after` 时间戳。命令层在注入 `/new`、`/clear`（以及会创建新 session 的 `/compact`）前登记该时间戳。Backend 在该标记存在时，仅从同 provider、同 cwd 且 mtime 不早于标记的 transcript 中选择最新文件；新身份被 tailer 发现、持久化后清除标记。没有标记时继续优先使用已持久化 identity。

这使由通道发起的会话切换可靠，同时不会将同项目目录的另一个 tmux binding 的既有 transcript 误判为当前 binding。

## 边界

- 新 transcript 尚未创建时，继续读取旧 transcript，不能提前解绑。
- 候选文件必须晚于命令登记时刻，且 session ID 不能等于旧 ID。
- `/resume` 是交互式挑选器，不自动切换；本次不改变它。
- 原生 tmux TUI 中绕开 bot 手动输入 `/new` 没有可验证的 pane-to-transcript 关联，不能安全地在同 cwd 多 binding 场景下自动认领；该场景维持持久化身份，后续可通过 provider 原生 hook/会话事件扩展。

## 验收

1. Codex/Claude 绑定在 `/new` 前固定到旧 transcript。
2. 登记 handoff 后，新 transcript 出现，选择器返回新 transcript。
3. jsonl tailer 将新 ID 与路径持久化，清除 handoff 标记。
4. 未登记 handoff 时仍返回旧 transcript，避免跨 binding 串消息。
5. 对新 identity 的 `ensure_running()` 生成对应 provider 的 resume 命令。
