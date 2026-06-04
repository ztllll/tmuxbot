"""Slash command adapter for provider TUI commands.

The provider remains the source of truth. This module classifies commands and
adds a thin transaction/interaction layer around tmux injection so IM users get
feedback for interactive or unknown slash commands.
"""
from __future__ import annotations

import asyncio
import hashlib
import html
import logging
import re
import time
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

from tmuxbot.tmux import tmux_capture, tmux_send_key, tmux_send_text
from tmuxbot.utils import strip_decorations

if TYPE_CHECKING:
    from tmuxbot.backends.base import Backend
    from tmuxbot.frontends.base import Frontend
    from tmuxbot.state import Binding, State

log = logging.getLogger("tmuxbot")


class CommandKind(str, Enum):
    LOCAL = "local"
    CAPTURE = "capture"
    INTERACTIVE = "interactive"
    PASSTHROUGH = "passthrough"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class ParsedSlash:
    command: str
    raw_command: str
    injected_text: str
    args: str


@dataclass(frozen=True)
class CommandSpec:
    command: str
    kind: CommandKind
    description: str = ""
    notice: str = ""
    lines: int = 90


@dataclass
class CommandTransaction:
    txn_id: str
    binding_name: str
    command: str
    kind: CommandKind
    injected_text: str
    started_at: float
    start_session_id: str | None
    start_pane_hash: str
    status: str = "started"


@dataclass(frozen=True)
class SemanticAction:
    action: str
    label: str
    keys: tuple[str, ...]
    description: str = ""


@dataclass(frozen=True)
class InteractionState:
    kind: str
    title: str
    hint: str
    actions: tuple[SemanticAction, ...] = ()


_LOCAL_COMMANDS = {"/screen", "/info", "/restart", "/esc", "/cc", "/eof"}

_BLOCKED_COMMANDS = {
    "/logout": "会清除本机 CLI 登录态, 已阻止。需要退出 CLI 请用 /eof, 需要重启用 /restart。",
    "/quit": "会退出当前 CLI, 已阻止。需要退出 CLI 请用 /eof, 需要重启用 /restart。",
    "/exit": "会退出当前 CLI, 已阻止。需要退出 CLI 请用 /eof, 需要重启用 /restart。",
}

_CLAUDE_INTERACTIVE = {
    "/add-dir": "添加工作目录, 可能打开交互确认。",
    "/agents": "打开 subagent 管理界面。",
    "/allowed-tools": "打开权限规则管理界面。",
    "/branch": "创建/切换会话分支, 可能打开选择界面。",
    "/code-review": "启动代码审查工作流。",
    "/debug": "打开调试/诊断信息。",
    "/diff": "显示当前 diff。",
    "/doctor": "运行安装/运行时诊断。",
    "/effort": "调整推理强度, 无参数时打开滑条。",
    "/mcp": "打开 MCP 状态/管理界面。",
    "/memory": "打开记忆管理界面。",
    "/model": "切换模型, 无参数时打开模型 picker。",
    "/permissions": "打开权限规则管理界面。",
    "/plan": "进入计划模式并让后续计划输出正常回推。",
    "/resume": "恢复历史会话, 无参数时打开会话 picker。",
    "/review": "启动审查工作流。",
    "/security-review": "启动安全审查工作流。",
    "/settings": "打开设置界面。",
    "/tasks": "打开后台任务列表。",
    "/ultraplan": "启动 ultraplan 工作流。",
    "/workflows": "打开 workflow 进度界面。",
}

_CODEX_INTERACTIVE = {
    "/agent": "切换/查看 agent thread。",
    "/apps": "打开 app/connector picker。",
    "/approve": "审批最近一次 auto review deny 的重试。",
    "/btw": "开启 side conversation。",
    "/debug-config": "显示配置层诊断。",
    "/diff": "显示当前 diff。",
    "/experimental": "打开实验功能开关。",
    "/fast": "切换或查看 Fast tier。",
    "/fork": "fork 当前会话。",
    "/goal": "设置或管理持久 goal。",
    "/hooks": "查看/管理 hooks。",
    "/ide": "引入 IDE 上下文。",
    "/keymap": "打开快捷键设置。",
    "/mcp": "查看 MCP 工具状态。",
    "/memories": "配置 memories。",
    "/mention": "选择/附加文件。",
    "/model": "切换模型, 无参数时打开模型 picker。",
    "/permissions": "切换 approval/sandbox 权限模式。",
    "/personality": "选择回复风格。",
    "/plan": "进入计划模式并可带 inline prompt。",
    "/plugins": "打开插件浏览/管理界面。",
    "/ps": "查看后台 terminal。",
    "/raw": "切换 raw scrollback。",
    "/resume": "恢复历史会话。",
    "/review": "启动 working tree review。",
    "/side": "开启 side conversation。",
    "/skills": "浏览/选择 skills。",
    "/statusline": "配置 status line。",
    "/stop": "停止后台 terminal。",
    "/theme": "选择终端主题。",
    "/title": "配置终端标题。",
    "/vim": "切换 composer Vim mode。",
}

_TUI_ACTIONS: dict[str, tuple[str | None, str]] = {
    "up": ("Up", "↑ Up"),
    "down": ("Down", "↓ Down"),
    "left": ("Left", "← Left"),
    "right": ("Right", "→ Right"),
    "enter": ("Enter", "Enter"),
    "tab": ("Tab", "Tab"),
    "space": ("Space", "Space"),
    "esc": ("Escape", "Escape"),
    "refresh": (None, "Refresh"),
}

_SEMANTIC_ACTIONS: dict[str, SemanticAction] = {
    "approve-plan": SemanticAction(
        "approve-plan", "批准计划", ("Enter",), "选择当前高亮的批准/继续选项。"
    ),
    "revise-plan": SemanticAction(
        "revise-plan", "继续修改", ("Down", "Enter"), "选择反馈/继续计划选项。"
    ),
    "reject-plan": SemanticAction(
        "reject-plan", "退出计划", ("Escape",), "取消当前计划审批界面。"
    ),
    "approve-once": SemanticAction(
        "approve-once", "批准一次", ("Enter",), "批准当前高亮的权限请求。"
    ),
    "deny": SemanticAction("deny", "拒绝", ("Escape",), "拒绝或关闭当前确认界面。"),
    "select-current": SemanticAction(
        "select-current", "选择当前项", ("Enter",), "选择当前高亮项。"
    ),
    "cancel": SemanticAction("cancel", "取消", ("Escape",), "取消当前 TUI 交互。"),
}

_TUI_COMMANDS = {
    "/up": "up",
    "/down": "down",
    "/left": "left",
    "/right": "right",
    "/enter": "enter",
    "/tab": "tab",
    "/space": "space",
    "/refresh": "refresh",
}

_SEMANTIC_COMMANDS = {
    "/approve-plan": "approve-plan",
    "/revise-plan": "revise-plan",
    "/reject-plan": "reject-plan",
    "/approve-once": "approve-once",
    "/deny": "deny",
    "/select": "select-current",
    "/cancel": "cancel",
}

_UNKNOWN_FAILURE_RE = re.compile(
    r"(unknown|invalid|unrecognized|not\s+recognized|no\s+command|"
    r"未识别|未知命令|无效命令)",
    re.I,
)


def parse_slash_text(
    text: str,
    *,
    bot_username: str | None = None,
    aliases: dict[str, str] | None = None,
) -> ParsedSlash | None:
    if not text.lstrip().startswith("/"):
        return None

    aliases = aliases or {}
    leading = len(text) - len(text.lstrip())
    stripped = text.lstrip()
    raw_cmd = stripped.split()[0]

    if bot_username and raw_cmd.lower().endswith(f"@{bot_username.lower()}"):
        command = raw_cmd[: -(len(bot_username) + 1)]
    else:
        command = raw_cmd.split("@", 1)[0]

    injected = text[:leading] + command + stripped[len(raw_cmd):]
    args = injected.lstrip()[len(command):].strip()

    real_cmd = aliases.get(command)
    if real_cmd:
        injected = text[:leading] + real_cmd + stripped[len(raw_cmd):]

    return ParsedSlash(command=command, raw_command=raw_cmd, injected_text=injected, args=args)


def classify_command(backend: "Backend", command: str) -> CommandSpec:
    if command in _BLOCKED_COMMANDS:
        return CommandSpec(command, CommandKind.BLOCKED, notice=_BLOCKED_COMMANDS[command])
    if (
        command in _LOCAL_COMMANDS
        or command in _TUI_COMMANDS
        or command in _SEMANTIC_COMMANDS
        or command == "/key"
    ):
        return CommandSpec(command, CommandKind.LOCAL)
    if command in backend.command_opts():
        return CommandSpec(command, CommandKind.CAPTURE)

    interactive = _CODEX_INTERACTIVE if backend.name == "codex" else _CLAUDE_INTERACTIVE
    if command in interactive:
        return CommandSpec(
            command,
            CommandKind.INTERACTIVE,
            description=interactive[command],
            notice=_interactive_notice(command, interactive[command]),
        )
    return CommandSpec(command, CommandKind.PASSTHROUGH)


def action_from_command(command: str, args: str) -> str | None:
    if command in _TUI_COMMANDS:
        return _TUI_COMMANDS[command]
    if command == "/key":
        key = args.strip().lower()
        aliases = {
            "return": "enter",
            "↵": "enter",
            "escape": "esc",
            " ": "space",
        }
        return aliases.get(key, key) if key else None
    return None


def semantic_action_from_command(command: str) -> str | None:
    return _SEMANTIC_COMMANDS.get(command)


def binding_token(binding_name: str) -> str:
    return hashlib.blake2s(binding_name.encode("utf-8"), digest_size=5).hexdigest()


def binding_by_token(bindings: list["Binding"], token: str) -> "Binding | None":
    for b in bindings:
        if binding_token(b.name) == token:
            return b
    return None


def tui_action_label(action: str) -> str:
    return _TUI_ACTIONS.get(action, (None, action))[1]


def available_tui_actions() -> dict[str, str]:
    return {k: v[1] for k, v in _TUI_ACTIONS.items()}


def semantic_actions_from_body(html_text: str) -> tuple[SemanticAction, ...]:
    actions: list[SemanticAction] = []
    for action in _SEMANTIC_ACTIONS.values():
        if f"/{action.action}" in html_text:
            actions.append(action)
    return tuple(actions)


def detect_interaction_state(raw: str) -> InteractionState:
    clean = strip_decorations(raw)
    low = clean.lower()

    if _looks_like_plan_approval(low):
        return InteractionState(
            kind="plan_approval",
            title="计划审批",
            hint="检测到计划审批界面。确认高亮项后可批准, 或继续给反馈。",
            actions=(
                _SEMANTIC_ACTIONS["approve-plan"],
                _SEMANTIC_ACTIONS["revise-plan"],
                _SEMANTIC_ACTIONS["reject-plan"],
            ),
        )
    if _looks_like_picker(low):
        return InteractionState(
            kind="picker",
            title="选择器",
            hint="检测到 TUI 选择器。可移动高亮项后选择。",
            actions=(
                _SEMANTIC_ACTIONS["select-current"],
                _SEMANTIC_ACTIONS["cancel"],
            ),
        )
    if _looks_like_permission_prompt(low):
        return InteractionState(
            kind="permission_prompt",
            title="权限确认",
            hint="检测到权限/审批提示。确认当前高亮项后再批准。",
            actions=(
                _SEMANTIC_ACTIONS["approve-once"],
                _SEMANTIC_ACTIONS["deny"],
            ),
        )
    return InteractionState(
        kind="generic",
        title="TUI 控制",
        hint="未识别到特定工作流, 使用通用按键操作。",
    )


def record_transaction(
    state: "State",
    b: "Binding",
    spec: CommandSpec,
    injected_text: str,
) -> CommandTransaction:
    pane = tmux_capture(b.tmux_target, 80)
    started = time.time()
    txn = CommandTransaction(
        txn_id=f"{binding_token(b.name)}-{int(started * 1000)}",
        binding_name=b.name,
        command=spec.command,
        kind=spec.kind,
        injected_text=injected_text,
        started_at=started,
        start_session_id=b.last_session_id,
        start_pane_hash=str(hash(pane)),
    )
    state.command_transactions[b.name] = txn
    return txn


async def handle_tui_action(
    frontend: "Frontend",
    b: "Binding",
    chat_id: int | str,
    thread_id: int | None,
    action: str,
    *,
    lines: int = 90,
) -> None:
    key, label = _TUI_ACTIONS.get(action, (None, ""))
    if action not in _TUI_ACTIONS:
        await frontend.send_html(
            chat_id,
            thread_id,
            "⚠️ <b>未知 TUI 按键</b>\n"
            "可用: <code>/up /down /left /right /tab /space /enter /refresh</code>",
        )
        return
    if key is not None:
        tmux_send_key(b.tmux_target, key)
        await asyncio.sleep(0.45)
    body = build_interaction_body(b, title=f"🎛 TUI 控制 · {html.escape(label)}", lines=lines)
    await frontend.send_interaction_card(chat_id, thread_id, body, b.name)


async def handle_semantic_action(
    frontend: "Frontend",
    b: "Binding",
    chat_id: int | str,
    thread_id: int | None,
    action: str,
    *,
    lines: int = 90,
) -> None:
    semantic = _SEMANTIC_ACTIONS.get(action)
    if semantic is None:
        await frontend.send_html(
            chat_id,
            thread_id,
            "⚠️ <b>未知语义操作</b>\n"
            "可用: <code>/approve-plan /revise-plan /reject-plan /approve-once /deny</code>",
        )
        return
    for key in semantic.keys:
        tmux_send_key(b.tmux_target, key)
        await asyncio.sleep(0.08)
    await asyncio.sleep(0.45)
    body = build_interaction_body(
        b,
        title=f"🎛 语义操作 · {html.escape(semantic.label)}",
        lines=lines,
    )
    await frontend.send_interaction_card(chat_id, thread_id, body, b.name)


async def handle_interactive_command(
    frontend: "Frontend",
    b: "Binding",
    state: "State",
    chat_id: int | str,
    thread_id: int | None,
    spec: CommandSpec,
    injected_text: str,
) -> None:
    txn = record_transaction(state, b, spec, injected_text)
    await tmux_send_text(b.tmux_target, injected_text)
    log.info("[%s] interactive command injected: %s txn=%s", b.name, spec.command, txn.txn_id)
    await asyncio.sleep(1.0)
    body = build_interaction_body(
        b,
        title=f"🎛 <b>{html.escape(spec.command)}</b> 已注入",
        note=spec.notice,
        lines=spec.lines,
    )
    await frontend.send_interaction_card(chat_id, thread_id, body, b.name)


async def handle_passthrough_command(
    frontend: "Frontend",
    b: "Binding",
    state: "State",
    chat_id: int | str,
    thread_id: int | None,
    spec: CommandSpec,
    injected_text: str,
) -> None:
    txn = record_transaction(state, b, spec, injected_text)
    before_hash = txn.start_pane_hash
    await tmux_send_text(b.tmux_target, injected_text)
    await frontend.send_html(
        chat_id,
        thread_id,
        f"↪️ <b>已透传未知命令</b> <code>{html.escape(spec.command)}</code>\n"
        "· 若 TUI 弹出选择/确认, 可用 <code>/refresh</code> 查看, "
        "再用 <code>/up /down /left /right /tab /space /enter</code> 操作。",
    )
    state.fire(
        probe_passthrough_result(frontend, b, chat_id, thread_id, spec.command, before_hash)
    )


async def probe_passthrough_result(
    frontend: "Frontend",
    b: "Binding",
    chat_id: int | str,
    thread_id: int | None,
    command: str,
    before_hash: str,
    *,
    delay: float = 1.4,
) -> None:
    await asyncio.sleep(delay)
    raw = tmux_capture(b.tmux_target, 90)
    out = strip_decorations(raw)
    if _UNKNOWN_FAILURE_RE.search(out):
        await frontend.send_html(
            chat_id,
            thread_id,
            f"⚠️ <b>{html.escape(command)} 可能被 TUI 拒绝</b>\n"
            f"<pre>{html.escape(_tail(out, 18))}</pre>",
        )
        return
    if str(hash(raw)) == before_hash:
        log.info("[%s] passthrough command produced no visible pane delta: %s", b.name, command)


def build_interaction_body(
    b: "Binding",
    *,
    title: str = "🎛 TUI 控制",
    note: str = "",
    lines: int = 90,
) -> str:
    raw = tmux_capture(b.tmux_target, lines)
    state = detect_interaction_state(raw)
    out = strip_decorations(raw)
    parts = [
        title,
        f"· binding <code>{html.escape(b.name)}</code>",
        f"· 状态 <b>{html.escape(state.title)}</b>: {html.escape(state.hint)}",
    ]
    if note:
        parts.append(f"· {html.escape(note)}")
    if state.actions:
        action_text = " / ".join(
            f"<code>/{html.escape(action.action)}</code> {html.escape(action.label)}"
            for action in state.actions
        )
        parts.append(f"· 语义操作: {action_text}")
    parts.extend(
        [
            "· 文字控制: <code>/up /down /left /right /tab /space /enter /refresh</code>",
            "",
            f"<pre>{html.escape(_tail(out, lines))}</pre>",
        ]
    )
    return "\n".join(parts)


def _interactive_notice(command: str, description: str) -> str:
    return (
        f"{description} 后续普通回复会继续按 jsonl 回推; "
        f"如果 {command} 打开选择或确认界面, 用下方按钮或 /key 命令操作。"
    )


def _tail(text: str, lines: int) -> str:
    if not text.strip():
        return "(empty screen)"
    rows = text.splitlines()
    return "\n".join(rows[-lines:])


def _looks_like_plan_approval(low: str) -> bool:
    return (
        ("plan" in low or "计划" in low)
        and (
            "approve" in low
            or "approval" in low
            or "start coding" in low
            or "accept edits" in low
            or "keep planning" in low
            or "批准" in low
            or "继续计划" in low
        )
    )


def _looks_like_permission_prompt(low: str) -> bool:
    return (
        "approve once" in low
        or "requires approval" in low
        or "permission required" in low
        or "requesting permission" in low
        or "approval required" in low
        or (
            "allow" in low
            and "deny" in low
            and ("command" in low or "tool" in low or "permission" in low)
        )
        or ("批准" in low and "拒绝" in low)
        or "批准一次" in low
    )


def _looks_like_picker(low: str) -> bool:
    return (
        "enter to select" in low
        or "esc to cancel" in low
        or "to navigate" in low
        or "↑/↓" in low
    )
