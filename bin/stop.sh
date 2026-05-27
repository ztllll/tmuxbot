#!/usr/bin/env bash
# 优雅停 tmuxbot
set -uo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

echo "[stop] 发 SIGTERM..."
pkill -TERM -f "python3 tmuxbot.py" 2>/dev/null || true
sleep 2

if pgrep -f "python3 tmuxbot.py" >/dev/null; then
    echo "[stop] 还活着, 发 SIGKILL..."
    pkill -KILL -f "python3 tmuxbot.py" 2>/dev/null || true
    sleep 1
fi

rm -f data/tmuxbot.lock
tmux kill-session -t tmuxbot-runner 2>/dev/null || true
echo "[stop] ✅ 停止"
