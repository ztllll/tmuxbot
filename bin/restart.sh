#!/usr/bin/env bash
# 重启 tmuxbot。已安装 systemd user service 时优先交给 systemd 管理；
# 否则回退到独立 tmux runner。
# 用法: bash bin/restart.sh
set -uo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

if systemctl --user cat tmuxbot.service >/dev/null 2>&1; then
    echo "[restart] 使用 systemd user service..."
    # 清理旧版本脚本可能遗留的 runner，避免两个实例争抢 Telegram getUpdates。
    tmux kill-session -t tmuxbot-runner 2>/dev/null || true
    rm -f data/tmuxbot.lock
    systemctl --user restart tmuxbot.service
    for i in 1 2 3 4 5; do
        if systemctl --user is-active --quiet tmuxbot.service; then
            echo "[restart] ✅ systemd bot 启动成功"
            systemctl --user status tmuxbot.service --no-pager -l | sed -n '1,12p'
            exit 0
        fi
        sleep 1
    done
    echo "[restart] ❌ systemd bot 启动失败"
    systemctl --user status tmuxbot.service --no-pager -l || true
    exit 1
fi

echo "[restart] 未安装 systemd service，使用 tmux runner..."
echo "[restart] 清 stale lock + 旧 tmux runner..."
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
