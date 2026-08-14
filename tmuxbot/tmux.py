"""tmux 低层封装: send / capture。

后端 (claude / codex) 共用。**注入文本是 async 函数**, 避免阻塞 event loop。
"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess

from tmuxbot.runtime.tmux_runtime import TmuxRuntime
from tmuxbot.utils import strip_decorations

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
# OMP screen text is a weak signal. A footer is current only when one known frame
# pair occupies the last two non-empty pane lines. Field parsing is semantic and
# lives in OmpBackend; this seam only proves that the candidate is the live footer.
_OMP_FOOTER_TOP_RE = re.compile(r"^\s*╭── π(?:\s.*)?╮\s*$")
_OMP_FOOTER_BOTTOM_RE = re.compile(r"^\s*╰─(?:.*)─╯\s*$")
_OMP_COMPACT_FOOTER_TOP_RE = re.compile(r"^\s*\+--\s+.+\s+--\+\s*$")
_OMP_COMPACT_FOOTER_BOTTOM_RE = re.compile(r"^\s*\+-[ \t]*-\+\s*$")
_OMP_STATUS_HINT_RE = re.compile(r"(?:⬢|\[M\]|\bctx\s*:|\d+(?:\.\d+)?%\s*/)", re.I)
_OMP_LIVE_LOADER_RE = re.compile(
    r"^\s*(?:[\u2801-\u28ff]|[-\\|/])[ \t]+.+?[ \t]*(?:⟦esc⟧|\[esc\])[ \t]*$",
    re.I,
)
_COMPOSER_SEPARATOR_RE = re.compile(r"^[─━═╌╍┄┅┈┉]{5,}$")
_CODEX_STATUS_RE = re.compile(
    r"^\s*gpt-[\w.-]+(?:\s+[\w-]+)?\s*[·•]\s*(?:~?/|/)\S+",
    re.I,
)


def find_omp_footer_pair(lines: list[str]) -> tuple[int, int] | None:
    """Locate a current supported OMP footer frame in already de-ANSI lines."""
    nonempty = [index for index, line in enumerate(lines) if line.strip()]
    if len(nonempty) < 2:
        return None
    top_index, bottom_index = nonempty[-2:]
    top = lines[top_index]
    bottom = lines[bottom_index]
    if _OMP_FOOTER_TOP_RE.fullmatch(top) and _OMP_FOOTER_BOTTOM_RE.fullmatch(bottom):
        return top_index, bottom_index
    if (
        _OMP_COMPACT_FOOTER_TOP_RE.fullmatch(top)
        and _OMP_COMPACT_FOOTER_BOTTOM_RE.fullmatch(bottom)
        and _OMP_STATUS_HINT_RE.search(top)
    ):
        return top_index, bottom_index
    return None


def omp_live_loader(pane: str) -> str | None:
    """Return only the live braille loader immediately before the OMP footer."""
    lines = strip_decorations(pane).splitlines()
    pair = find_omp_footer_pair(lines)
    if pair is None:
        return None
    top_index, _ = pair
    loader_index = next(
        (index for index in range(top_index - 1, -1, -1) if lines[index].strip()),
        None,
    )
    if loader_index is None or not _OMP_LIVE_LOADER_RE.fullmatch(lines[loader_index]):
        return None
    return lines[loader_index].strip()


def _tmux(*args: str) -> subprocess.CompletedProcess:
    """同步短 tmux 调用 (查询类: has-session / display-message / capture-pane)。
    单次 < 50ms, 在 async 里偶尔调用不阻塞。"""
    return subprocess.run([TMUX, *args], capture_output=True, text=True)


def tmux_has_session(s: str) -> bool:
    return _tmux("has-session", "-t", s).returncode == 0


def tmux_new_session(s: str, cwd) -> None:
    _tmux("new-session", "-d", "-s", s, "-c", str(cwd))


def tmux_respawn_pane(target: str, cwd) -> bool:
    """Replace one pane process tree with its configured shell.

    Callers must establish either an unsafe provider tree or an explicit operator
    restart. The operation targets one pane and cannot affect another route pane.
    """
    return _tmux("respawn-pane", "-k", "-t", target, "-c", str(cwd)).returncode == 0


def tmux_error_is_no_server(value: str | bytes) -> bool:
    """Match only tmux's standard no-server diagnostic, with no extra errors."""
    text = value.decode(errors="replace") if isinstance(value, bytes) else value
    if text.endswith("\r\n"):
        text = text[:-2]
    elif text.endswith("\n"):
        text = text[:-1]
    lowered = text.lower()
    if any(
        marker in lowered
        for marker in ("permission denied", "access denied", "authentication failed")
    ):
        return False
    return bool(re.fullmatch(r"no server running on /\S+", text))


def tmux_kill_session(s: str) -> bool:
    """Ensure a tmux session is stopped, distinguishing absence from real failure."""
    try:
        result = _tmux("kill-session", "-t", s)
    except OSError:
        return False
    if result.returncode == 0:
        return True
    stderr = result.stderr or ""
    if tmux_error_is_no_server(stderr):
        return True
    if stderr.endswith("\r\n"):
        stderr = stderr[:-2]
    elif stderr.endswith("\n"):
        stderr = stderr[:-1]
    return stderr == f"can't find session: {s}"


def tmux_pane_command(target: str) -> str:
    r = _tmux("display-message", "-t", target, "-p", "#{pane_current_command}")
    return r.stdout.strip()


def tmux_pane_pid(target: str) -> int | None:
    """Return the pane shell PID, or ``None`` when the target is unavailable."""
    result = _tmux("display-message", "-t", target, "-p", "#{pane_pid}")
    try:
        return int(result.stdout.strip())
    except ValueError:
        return None


def tmux_pane_process_commands(target: str) -> tuple[str, ...]:
    """Return command lines for a pane shell and all of its live descendants."""
    pane = _tmux("display-message", "-t", target, "-p", "#{pane_pid}")
    try:
        root_pid = int(pane.stdout.strip())
    except ValueError:
        return ()
    processes = subprocess.run(["ps", "-eo", "pid=,ppid=,args="], capture_output=True, text=True)
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
    """判断 Claude/Codex/OMP TUI 当前是否 busy。"""
    clean = strip_decorations(pane)
    return bool(_TUI_BUSY_RE.search(clean) or omp_live_loader(clean))


def _active_input_text(pane: str) -> str | None:
    """Read only the active Claude/Codex/OMP composer, excluding prompt history."""
    lines = strip_decorations(pane).splitlines()

    omp_pair = find_omp_footer_pair(lines)
    if omp_pair is not None:
        footer_top, _ = omp_pair
        separators = [
            index
            for index, line in enumerate(lines[:footer_top])
            if _COMPOSER_SEPARATOR_RE.fullmatch(line.strip())
        ]
        if len(separators) >= 2:
            composer = lines[separators[-2] + 1 : separators[-1]]
            return "\n".join(line.strip() for line in composer if line.strip())
        return None

    separators = [
        index for index, line in enumerate(lines) if _COMPOSER_SEPARATOR_RE.fullmatch(line.strip())
    ]
    if len(separators) >= 2:
        composer = lines[separators[-2] + 1 : separators[-1]]
        claude_input = _composer_body(composer, "❯")
        if claude_input is not None:
            return claude_input

    status_index = next(
        (index for index in range(len(lines) - 1, -1, -1) if _CODEX_STATUS_RE.match(lines[index])),
        None,
    )
    if status_index is None:
        return None
    prompt_index = next(
        (
            index
            for index in range(status_index - 1, -1, -1)
            if lines[index].lstrip().startswith("›")
        ),
        None,
    )
    if prompt_index is None:
        return None
    return _composer_body(lines[prompt_index:status_index], "›")


def _composer_body(lines: list[str], marker: str) -> str | None:
    first_index = next(
        (index for index, line in enumerate(lines) if line.lstrip().startswith(marker)),
        None,
    )
    if first_index is None:
        return None
    first = lines[first_index].lstrip()[len(marker) :].strip()
    body = [first] if first else []
    body.extend(line.strip() for line in lines[first_index + 1 :] if line.strip())
    return "\n".join(body)


async def tmux_send_text(
    target: str,
    text: str,
    *,
    with_enter: bool = True,
    expected_commands=None,
    allow_busy_submission: bool = False,
) -> None:
    """Queue input, optionally preserve provider-native busy input, and verify submission."""
    await _RUNTIME.send_text(
        target,
        text,
        with_enter=with_enter,
        expected_commands=expected_commands,
        allow_busy_submission=allow_busy_submission,
    )


async def tmux_safe_launch(target: str, command: str, *, allowed_shells) -> bool:
    """Launch only while the pane remains attached to an allowed shell."""
    return await _RUNTIME.safe_launch(target, command, allowed_shells=allowed_shells)


async def _paste_text(target: str, text: str) -> None:
    buf = f"tb_{os.getpid()}"
    load_proc = await asyncio.create_subprocess_exec(
        TMUX,
        "load-buffer",
        "-b",
        buf,
        "-",
        stdin=asyncio.subprocess.PIPE,
    )
    await load_proc.communicate(input=text.encode("utf-8"))
    paste_proc = await asyncio.create_subprocess_exec(
        TMUX,
        "paste-buffer",
        "-b",
        buf,
        "-t",
        target,
        "-p",
        "-d",
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
    input_reader=_active_input_text,
)
