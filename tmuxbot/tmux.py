"""tmux 低层封装: send / capture。

后端 (claude / codex) 共用。**注入文本是 async 函数**, 避免阻塞 event loop。
"""
from __future__ import annotations

import asyncio
import logging
import os
import subprocess

log = logging.getLogger("tmuxbot")

TMUX = "tmux"
SEND_KEYS_DELAY = 0.5   # paste 完到 Enter 之间(TUI 渲染窗口)


def _tmux(*args: str) -> subprocess.CompletedProcess:
    """同步短 tmux 调用 (查询类: has-session / display-message / capture-pane)。
    单次 < 50ms, 在 async 里偶尔调用不阻塞。"""
    return subprocess.run([TMUX, *args], capture_output=True, text=True)


def tmux_has_session(s: str) -> bool:
    return _tmux("has-session", "-t", s).returncode == 0


def tmux_new_session(s: str, cwd) -> None:
    _tmux("new-session", "-d", "-s", s, "-c", str(cwd))


def tmux_pane_command(target: str) -> str:
    r = _tmux("display-message", "-t", target, "-p", "#{pane_current_command}")
    return r.stdout.strip()


def tmux_send_key(target: str, key: str) -> None:
    _tmux("send-keys", "-t", target, key)


def tmux_capture(target: str, lines: int = 50) -> str:
    r = _tmux("capture-pane", "-t", target, "-p", "-S", f"-{lines}")
    return r.stdout


async def tmux_send_text(target: str, text: str, *, with_enter: bool = True) -> None:
    """文本注入: paste-buffer -p (bracketed paste) + await sleep + Enter。
    全程 async, 不阻塞 event loop (旧版 sync sleep 是反模式)。
    不前置 Esc: Boss 发消息时不应中断 claude 当前生成。
    需要显式打断/退 modal: 用 /esc 或 /cc 命令。
    capture_and_push 抓完后会自己 Esc 关 modal, 所以下一条消息不会卡在 picker。"""
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
    if with_enter:
        await asyncio.sleep(SEND_KEYS_DELAY)
        tmux_send_key(target, "Enter")
