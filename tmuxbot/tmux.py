"""tmux 低层封装: send / capture。

后端 (claude / codex) 共用。**注入文本是 async 函数**, 避免阻塞 event loop。
"""
from __future__ import annotations

import asyncio
import os
import re
import subprocess

from tmuxbot.runtime.tmux_runtime import TmuxRuntime

TMUX = "tmux"
IDLE_WAIT_MAX = 300.0
IDLE_POLL_INTERVAL = 0.25
POST_PASTE_DELAY = 0.5

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


def tmux_pane_process_commands(target: str) -> tuple[str, ...]:
    """Return command lines for a pane shell and all of its live descendants."""
    pane = _tmux("display-message", "-t", target, "-p", "#{pane_pid}")
    try:
        root_pid = int(pane.stdout.strip())
    except ValueError:
        return ()
    processes = subprocess.run(
        ["ps", "-eo", "pid=,ppid=,args="], capture_output=True, text=True
    )
    children: dict[int, list[tuple[int, str]]] = {}
    for line in processes.stdout.splitlines():
        fields = line.strip().split(maxsplit=2)
        if len(fields) != 3:
            continue
        try:
            pid, ppid = int(fields[0]), int(fields[1])
        except ValueError:
            continue
        children.setdefault(ppid, []).append((pid, fields[2]))
    pending = [root_pid]
    commands: list[str] = []
    while pending:
        parent = pending.pop()
        for pid, command in children.get(parent, []):
            commands.append(command)
            pending.append(pid)
    return tuple(commands)


def tmux_send_key(target: str, key: str) -> None:
    _tmux("send-keys", "-t", target, key)


def tmux_capture(target: str, lines: int = 50) -> str:
    r = _tmux("capture-pane", "-t", target, "-p", "-S", f"-{lines}")
    return r.stdout


def _is_tui_busy(pane: str) -> bool:
    """判断 claude/codex TUI 当前是否 busy (屏幕底部有"动词 + 时间"状态行)"""
    return bool(_TUI_BUSY_RE.search(pane))


async def tmux_send_text(
    target: str,
    text: str,
    *,
    with_enter: bool = True,
    expected_commands=None,
) -> None:
    """Queue input, wait for an idle pane, then paste and submit exactly once."""
    await _RUNTIME.send_text(
        target,
        text,
        with_enter=with_enter,
        expected_commands=expected_commands,
    )


async def tmux_safe_launch(target: str, command: str, *, allowed_shells) -> bool:
    """Launch only while the pane remains attached to an allowed shell."""
    return await _RUNTIME.safe_launch(target, command, allowed_shells=allowed_shells)


async def _paste_text(target: str, text: str) -> None:
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


_RUNTIME = TmuxRuntime(
    capture_func=tmux_capture,
    pane_command_func=tmux_pane_command,
    paste_func=_paste_text,
    send_key_func=tmux_send_key,
    busy_detector=_is_tui_busy,
    poll_interval=IDLE_POLL_INTERVAL,
    wait_timeout=IDLE_WAIT_MAX,
    post_paste_delay=POST_PASTE_DELAY,
)
