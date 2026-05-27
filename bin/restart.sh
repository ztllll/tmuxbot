#!/usr/bin/env bash
# 重启 tmuxbot — 解决"第一次 new-session 总失败"的小毛病。
# 用法: bash bin/restart.sh
set -uo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

echo "[restart] kill 旧 bot 进程..."
pkill -KILL -f "python3 tmuxbot.py" 2>/dev/null || true
sleep 1

echo "[restart] 清 stale lock + 旧 tmux session..."
rm -f data/tmuxbot.lock
tmux kill-session -t tmuxbot-runner 2>/dev/null || true
sleep 1

mkdir -p data

# 第一次 new-session 偶尔失败 (tmux server 状态过渡), 重试最多 3 次
for i in 1 2 3; do
    if tmux new-session -d -s tmuxbot-runner -x 156 -y 40 \
        "python3 tmuxbot.py 2>&1 | tee -a data/tmuxbot.log"; then
        sleep 3
        if pgrep -f "python3 tmuxbot.py" >/dev/null; then
            echo "[restart] ✅ bot 启动成功 (尝试 $i 次)"
            grep -E "starting|backend=|polling" data/tmuxbot.log | tail -8
            exit 0
        fi
    fi
    echo "[restart] 第 $i 次未起来, 重试..."
    rm -f data/tmuxbot.lock
    tmux kill-session -t tmuxbot-runner 2>/dev/null || true
    sleep 2
done

echo "[restart] ❌ 3 次都失败, 看 data/tmuxbot.log 排查"
exit 1
