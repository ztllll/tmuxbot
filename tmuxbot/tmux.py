"""tmux 低层封装: send / capture。

后端 (claude / codex) 共用。**注入文本是 async 函数**, 避免阻塞 event loop。
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import subprocess

log = logging.getLogger("tmuxbot")

TMUX = "tmux"
SEND_KEYS_DELAY = 0.5    # paste 完到 Enter 之间(TUI 渲染窗口, idle 态下兜底)
IDLE_WAIT_MAX = 10.0     # 等 claude TUI idle 最长秒数(busy 时 Enter 会丢)
IDLE_POLL_INTERVAL = 0.25

# claude / codex TUI busy 状态行 = 动词 + **括号包裹**的时间字段 (进行中标记):
#   claude:  "✶ Doing… (4m 4s · ↓ 14.3k tokens)"   "Cooking up… (12s)"
#   codex:   "• Working (9s • esc to interrupt)"
# **关键**: idle 后的历史标记是 "for Xs" 格式 (无括号), 不算 busy:
#   "✻ Sautéed for 4m 47s"   "* Crunched for 3m 1s"
# 早期版本 regex 没区分, 误把 "Sautéed for Xs" 当 busy, 导致 tmux_send_text 卡 10s 假等。
_TUI_BUSY_VERBS = r"(?:Working|Doing|Crunching|Thinking|Generating|Pondering|Reasoning|Cooking|Brewing|Simmering|Reading|Searching|Loading|Analyzing|Processing|Querying)"
_TUI_BUSY_RE = re.compile(
    _TUI_BUSY_VERBS + r"[…\.]*\s*[^\n]{0,30}?\(\s*\d+(?:m\s+\d+)?s",  # 必须 ( 开头时间
    re.I,
)


def _tmux(*args: str) -> subprocess.CompletedProcess:
    """同步短 tmux 调用 (查询类: has-session / display-message / capture-pane)。
    单次 < 50ms, 在 async 里偶尔调用不阻塞。"""
    return subprocess.run([TMUX, *args], capture_output=True, text=True)


def tmux_has_session(s: str) -> bool:
    return _tmux("has-session", "-t", s).returncode == 0


def tmux_new_session(s: str, cwd) -> None:
    _tmux("new-session", "-d", "-s", s, "-c", str(cwd))


def tmux_kill_session(s: str) -> bool:
    """杀掉整个 tmux session (deprovision 用)。session 名可能含中文/空格,
    交给 _tmux 以参数数组方式传, 不走 shell 不需手动引号。返回是否成功。"""
    return _tmux("kill-session", "-t", s).returncode == 0


def tmux_pane_command(target: str) -> str:
    r = _tmux("display-message", "-t", target, "-p", "#{pane_current_command}")
    return r.stdout.strip()


def tmux_send_key(target: str, key: str) -> None:
    _tmux("send-keys", "-t", target, key)


def tmux_capture(target: str, lines: int = 50) -> str:
    r = _tmux("capture-pane", "-t", target, "-p", "-S", f"-{lines}")
    return r.stdout


def _is_tui_busy(pane: str) -> bool:
    """判断 claude/codex TUI 当前是否 busy (屏幕底部有"动词 + 时间"状态行)"""
    return bool(_TUI_BUSY_RE.search(pane))


async def tmux_send_text(target: str, text: str, *, with_enter: bool = True) -> None:
    """文本注入: paste-buffer -p (bracketed paste) + 等 TUI idle + Enter。

    ★ 关键: claude busy 态下直接 send Enter 会进 PTY buffer 排队, 切回 idle 时
    Enter 可能被 paste 上下文吃掉 (历史踩坑 — 图片+文字一起发卡住的 race)。
    修复: paste 后**轮询 capture-pane**, 等 _TUI_BUSY_RE 不再命中 (TUI idle)
    才发 Enter。超时兜底 10s 强发 (适合极长 task 的情况)。

    不前置 Esc: Boss 发消息时不应中断 claude 当前生成。
    需要显式打断/退 modal: 用 /esc 或 /cc 命令。"""
    buf = f"tb_{os.getpid()}"
    load_proc = await asyncio.create_subprocess_exec(
        TMUX, "load-buffer", "-b", buf, "-",
        stdin=asyncio.subprocess.PIPE,
    )
    await load_proc.communicate(input=text.encode("utf-8"))
    paste_proc = await asyncio.create_subprocess_exec(
        TMUX, "paste-buffer", "-b", buf, "-t", target, "-p", "-d",
    )
    await paste_proc.wait()

    if not with_enter:
        return

    # 等 TUI idle 再发 Enter, 避开 busy 态下 PTY buffer race
    elapsed = 0.0
    saw_busy = False
    while elapsed < IDLE_WAIT_MAX:
        await asyncio.sleep(IDLE_POLL_INTERVAL)
        elapsed += IDLE_POLL_INTERVAL
        try:
            pane = tmux_capture(target, lines=15)
        except Exception:
            break
        if _is_tui_busy(pane):
            saw_busy = True
            continue
        # idle, 但是如果刚 paste 进去, TUI 可能还没渲染 — 给 SEND_KEYS_DELAY 兜底
        if not saw_busy and elapsed < SEND_KEYS_DELAY:
            continue
        break
    if elapsed >= IDLE_WAIT_MAX:
        log.warning(f"tmux_send_text: TUI busy >{IDLE_WAIT_MAX}s 仍未 idle, 强发 Enter (可能丢)")
    tmux_send_key(target, "Enter")
