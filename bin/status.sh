#!/usr/bin/env bash
# 看 tmuxbot 跑得怎样
set -uo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

echo "=== 进程 ==="
ps -ef | grep "[p]ython3 tmuxbot.py" || echo "(没有 bot 进程)"
echo ""
echo "=== tmux session ==="
tmux list-sessions 2>&1 | grep tmuxbot-runner || echo "(没有 tmuxbot-runner session)"
echo ""
echo "=== 日志末尾 ==="
tail -10 data/tmuxbot.log 2>/dev/null || echo "(无日志)"
echo ""
echo "=== 看 frontend / heartbeat 心跳 ==="
grep -E "starting|backend=|heartbeat|tailer alive" data/tmuxbot.log 2>/dev/null | tail -8
