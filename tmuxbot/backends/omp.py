"""OMP interactive TUI backend.

OMP remains a real tmux TUI.  This adapter only knows how to launch/resume OMP,
find its local session JSONL, normalize transcript rows, and read terminal state.
It never uses OMP print, RPC, SDK, or server modes.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
import shlex
import time
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from tmuxbot.backends.base import Backend, CmdOpts
from tmuxbot.core.capabilities import ProviderCapabilities
from tmuxbot.core.events import (
    ProviderEvent,
    ProviderEventKind,
    ProviderRuntimeMetadata,
    TerminalState,
    TerminalStatus,
)
from tmuxbot.core.sessions import SessionIdentity
from tmuxbot.providers.adapters import provider_launch_arguments
from tmuxbot.providers.discovery import ProviderDiscovery
from tmuxbot.runtime.omp_errors import is_user_abort_error
from tmuxbot.runtime.omp_handoff import read_handoff, read_session_header
from tmuxbot.runtime.omp_plan_mode import (
    PlanModeSnapshot,
    current_jsonl_branch,
    current_jsonl_branch_from_entries,
    plan_text_from_write_block,
    read_jsonl_entries,
    read_plan_mode_snapshot,
)
from tmuxbot.runtime.omp_session_health import read_session_health
from tmuxbot.runtime.omp_interaction import omp_ssh_interaction_notice
from tmuxbot.runtime.route_health import provider_tree_is_safe
from tmuxbot.tmux import (
    find_omp_footer_pair,
    omp_live_loader,
    tmux_has_session,
    tmux_new_session,
    tmux_pane_command,
    tmux_respawn_pane,
    tmux_safe_launch,
)
from tmuxbot.utils import strip_decorations

if TYPE_CHECKING:
    from tmuxbot.state import Binding

log = logging.getLogger("tmuxbot")

# OMP screen text is deliberately a weak signal. The tmux seam proves that the
# candidate is the live footer; these patterns extract semantic fields without
# depending on one version's frame, icon set, or field order.
_OMP_CONTEXT_RE = re.compile(
    r"(?:(?P<percent>\d+(?:\.\d+)?)%|\?)\s*/\s*"
    r"(?P<limit>\d+(?:\.\d+)?[kKmM]?)(?:\s*(?P<auto>\(auto\)|⟲|\[A\]|auto))?",
    re.I,
)
_OMP_COST_RE = re.compile(
    r"(?:^|\s|[•·<>-])\$(?P<value>\d+(?:\.\d+)?)(?P<subscription>\s+\(sub\))?"
)
_OMP_SESSION_TOKENS_RE = re.compile(
    r"(?:^|\s|[•·<>-])(?:tok(?:ens?)?\s*[:=])\s*(?P<value>\d+(?:\.\d+)?[kKmM]?)",
    re.I,
)
_OMP_EFFORT_RE = re.compile(
    r"(?:^|[•·<>-]\s*)(?:effort|thinking)\s*[:=]?\s*"
    r"(?P<effort>off|minimal|low|medium|high|xhigh|max|auto)"
    r"(?=\s*(?:[•·<>-]|$))",
    re.I,
)
_OMP_ICON_MODEL_RE = re.compile(
    r"(?:⬢|\[M\]|model\s*[:=])\s*(?P<model>.+?)\s+"
    r"\((?P<provider>[^()]+)\)(?=\s*(?:[·><-]|$))",
    re.I,
)
_OMP_LABELED_MODEL_RE = re.compile(
    r"(?:⬢|\[M\]|model\s*[:=])\s*(?P<model>.+?)(?=\s*(?:[·><-]|$))",
    re.I,
)
_OMP_ICON_EFFORT_RE = re.compile(
    r"(?:◕\s*(?P<effort>off|minimal|low|medium|high|xhigh|max|auto)\b|"
    r"\[(?P<short>off|min|low|med|hi|xhi|max|auto)\])",
    re.I,
)
_OMP_ICON_CWD_RE = re.compile(
    r"(?:📁|\[D\])\s*(?P<cwd>.+?)(?=\s*(?:>\s*)?(?:⑂|@|◫|◕|\$|ctx\s*:)|\s*[•·<]|$)",
    re.I,
)
_OMP_ICON_BRANCH_RE = re.compile(r"(?:⑂|@)\s*(?P<branch>\S+)")
_OMP_LOCATION_RE = re.compile(
    r"(?:^|[•·]\s*)(?P<cwd>~(?:/[^\s•·()]*)?|/(?:[^\s•·()]+))"
    r"(?:\s+\((?P<branch>[^()\n]+)\))?(?=\s*(?:[•·]|$))"
)
_OMP_SESSION_LABEL_RE = re.compile(
    r"(?:^|[•·]\s*)(?:plan|session)\s*[:#]\s*(?P<label>[^•·]+)",
    re.I,
)
_OMP_ACTIVE_LABEL_RE = re.compile(r"▶[─━-]*◀\s*(?P<label>.+?)\s*$")
_OMP_COMPACT_LABEL_RE = re.compile(r"@\s*\S+\s*-\s*(?P<label>.+?)\s*<\s*tok\s*:", re.I)

_OMP_EFFORT_ALIASES = {
    "min": "minimal",
    "med": "medium",
    "hi": "high",
    "xhi": "xhigh",
}


def _normalize_omp_effort(match: re.Match[str] | None) -> str | None:
    if match is None:
        return None
    raw = (match.groupdict().get("effort") or match.groupdict().get("short") or "").lower()
    return _OMP_EFFORT_ALIASES.get(raw, raw) or None


def _omp_footer_content(line: str) -> str:
    value = line.strip()
    if value.startswith("╭── π"):
        value = re.sub(r"^╭── π\s*|\s*╮$", "", value)
    elif value.startswith("╰─"):
        value = re.sub(r"^╰─\s*|\s*─╯$", "", value)
    elif value.startswith("+--"):
        value = re.sub(r"^\+--\s*|\s*--\+$", "", value)
    elif value.startswith("+-"):
        return ""
    return value.strip(" ─-")


def _start_cmd(transcript_path: str | Path | None = None) -> str:
    executable = ProviderDiscovery.resolve_executable("omp")
    if executable is None:
        raise RuntimeError("OMP executable not found")
    launch_arguments = provider_launch_arguments("omp")
    if launch_arguments is None:
        raise RuntimeError("OMP provider adapter is not registered")
    arguments = [executable, *launch_arguments]
    if transcript_path is not None:
        try:
            transcript = Path(transcript_path).expanduser().resolve(strict=True)
        except OSError as exc:
            raise RuntimeError(f"OMP transcript pin is unavailable: {transcript_path}") from exc
        if not transcript.is_file():
            raise RuntimeError(f"OMP transcript pin is not a file: {transcript}")
        arguments.extend(("--resume", str(transcript)))
    return " ".join(shlex.quote(argument) for argument in arguments)


def _omp_timestamp(value: Any) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _scaled_number(value: str) -> int:
    value = value.strip().lower()
    scale = 1
    if value.endswith("k"):
        scale, value = 1_000, value[:-1]
    elif value.endswith("m"):
        scale, value = 1_000_000, value[:-1]
    try:
        return int(float(value) * scale)
    except ValueError:
        return 0


def _format_omp_tokens(count: int) -> str:
    if count < 1_000:
        return str(count)
    if count < 10_000:
        return f"{count / 1_000:.1f}k"
    if count < 1_000_000:
        return f"{round(count / 1_000)}k"
    if count < 10_000_000:
        return f"{count / 1_000_000:.1f}M"
    return f"{round(count / 1_000_000)}M"


def _format_omp_status_percent(value: float) -> str:
    return str(int(value)) if value.is_integer() else f"{value:.1f}"


def _format_omp_context_limit(count: int) -> str:
    if count >= 1_000_000 and count % 1_000_000 == 0:
        return f"{count // 1_000_000}M"
    if count >= 1_000 and count % 1_000 == 0:
        return f"{count // 1_000}K"
    return _format_omp_tokens(count).replace("k", "K")


def _format_omp_file_size(size_bytes: int) -> str:
    if size_bytes >= 1_000_000:
        return f"{size_bytes / 1_000_000:.1f}MB"
    if size_bytes >= 1_000:
        return f"{size_bytes / 1_000:.1f}KB"
    return f"{size_bytes}B"


def _parse_omp_location_line(line: str) -> tuple[str | None, str | None, str | None]:
    value = line.strip()
    if not value or not value.startswith(("/", "~")):
        return None, None, None
    location, separator, session_name = value.partition(" • ")
    branch = None
    branch_match = re.fullmatch(r"(.+?)\s+\(([^()\n]+)\)", location)
    if branch_match:
        location = branch_match.group(1).strip()
        branch = branch_match.group(2).strip()
    return location, branch, session_name.strip() if separator else None


def _usage_cost(usage: Any) -> float:
    if not isinstance(usage, dict):
        return 0.0
    cost = usage.get("cost") or {}
    if isinstance(cost, dict):
        try:
            return float(cost.get("total", 0) or 0)
        except (TypeError, ValueError):
            return 0.0
    return 0.0


def _split_model_reference(value: Any) -> tuple[str, str] | None:
    if not isinstance(value, str):
        return None
    provider, separator, model = value.strip().partition("/")
    if not separator or not provider or not model:
        return None
    return provider, model


def _usage_int(usage: dict[str, Any], key: str) -> int:
    try:
        return int(usage.get(key, 0) or 0)
    except (TypeError, ValueError):
        return 0


def _normalize_todo_phases(value: Any) -> list[dict[str, str]] | None:
    if not isinstance(value, list):
        return None
    allowed = {"pending", "in_progress", "completed", "abandoned", "blocked"}
    flattened: list[dict[str, str]] = []
    for phase in value:
        if not isinstance(phase, dict):
            return None
        phase_name = phase.get("name")
        tasks = phase.get("tasks")
        if not isinstance(phase_name, str) or not phase_name.strip() or not isinstance(tasks, list):
            return None
        for task in tasks:
            if not isinstance(task, dict):
                return None
            content = task.get("content")
            status = task.get("status")
            blocker = task.get("blocker")
            if (
                not isinstance(content, str)
                or not content.strip()
                or status not in allowed
                or (blocker is not None and not isinstance(blocker, str))
            ):
                return None
            item = {
                "phase": phase_name.strip(),
                "content": content.strip(),
                "status": status,
            }
            if isinstance(blocker, str) and blocker.strip():
                item["blocker"] = blocker.strip()
            flattened.append(item)
    return flattened


def _format_tool(name: str, arguments: Any) -> str:
    labels = {
        "read": "📖 读取",
        "write": "✏️ 写入",
        "edit": "✂️ 编辑",
        "bash": "💻 执行",
        "todo": "📋 任务",
    }
    label = labels.get(name.lower(), f"🛠 {name}")
    if not isinstance(arguments, dict):
        return label
    detail = (
        arguments.get("path")
        or arguments.get("file_path")
        or arguments.get("command")
        or arguments.get("query")
    )
    return f"{label} <code>{html.escape(str(detail)[:180])}</code>" if detail else label


_OMP_MUTATION_PREVIEW_CHARS = 1_200
_OMP_MUTATION_PREVIEW_LINES = 48
_OMP_MUTATION_PREVIEW_FILES = 2
_OMP_PENDING_WRITE_LIMIT = 128
_OMP_SECRET_NAME_RE = re.compile(
    r"(^|[._-])(secret|secrets|credential|credentials|token|tokens|password|passwd)([._-]|$)",
    re.I,
)


def _safe_mutation_preview_path(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    path = value.strip()
    if not path or "://" in path:
        return None
    normalized = path.replace("\\", "/").lower()
    parts = tuple(part for part in normalized.split("/") if part)
    if not parts:
        return None
    name = parts[-1]
    if (
        name.startswith(".env")
        or name in {"bindings.yaml", "bindings.yml", "id_rsa", "id_ed25519"}
        or name.endswith((".key", ".pem", ".p12", ".pfx"))
        or any(part in {".ssh", ".gnupg"} for part in parts)
        or _OMP_SECRET_NAME_RE.search(name)
    ):
        return None
    return path


def _bounded_mutation_preview(value: str) -> str:
    lines = value.strip("\n").splitlines()
    truncated = len(lines) > _OMP_MUTATION_PREVIEW_LINES
    preview = "\n".join(lines[:_OMP_MUTATION_PREVIEW_LINES])
    if len(preview) > _OMP_MUTATION_PREVIEW_CHARS:
        preview = preview[:_OMP_MUTATION_PREVIEW_CHARS]
        if "\n" in preview:
            preview = preview.rsplit("\n", 1)[0]
        truncated = True
    if truncated:
        preview = f"{preview.rstrip()}\n… 已截断"
    return preview


def _format_edit_result_preview(message: dict[str, Any]) -> str | None:
    details = message.get("details")
    if not isinstance(details, dict):
        return None
    results = details.get("perFileResults")
    if not isinstance(results, list):
        return None

    previews: list[tuple[str, str]] = []
    for result in results:
        if not isinstance(result, dict):
            continue
        path = _safe_mutation_preview_path(result.get("path"))
        diff = result.get("diff")
        if path is None or not isinstance(diff, str) or not diff.strip():
            continue
        previews.append((path, _bounded_mutation_preview(diff)))

    if not previews:
        return None
    parts = ["✅ <b>代码 diff</b>"]
    for path, preview in previews[:_OMP_MUTATION_PREVIEW_FILES]:
        parts.append(f"<code>{html.escape(path[:240])}</code>\n<pre>{html.escape(preview)}</pre>")
    remaining = len(previews) - _OMP_MUTATION_PREVIEW_FILES
    if remaining > 0:
        parts.append(f"<i>另有 {remaining} 个文件 diff 已省略</i>")
    return "\n".join(parts)


def _format_write_result_preview(path: str, preview: str) -> str:
    return (
        f"✅ <b>写入代码片段</b> <code>{html.escape(path[:240])}</code>\n"
        f"<pre>{html.escape(preview)}</pre>"
    )


def _mutation_start(name: str, arguments: Any) -> tuple[str, str] | None:
    if not isinstance(arguments, dict):
        return None
    tool_name = name.lower()
    paths: list[str] = []
    if tool_name == "write":
        path = _safe_mutation_preview_path(arguments.get("path"))
        content = arguments.get("content")
        if path is None or not isinstance(content, str) or not content:
            return None
        paths.append(path)
        action = "✏️ 正在写入"
    elif tool_name == "edit":
        patch = arguments.get("input")
        if not isinstance(patch, str) or not patch:
            return None
        for match in re.finditer(r"(?m)^\[(?P<path>.+?)#[0-9A-Fa-f]{4}\]\s*$", patch):
            path = _safe_mutation_preview_path(match.group("path"))
            if path is not None and path not in paths:
                paths.append(path)
        if not paths:
            return None
        action = "✂️ 正在编辑"
    else:
        return None

    visible_paths = paths[:_OMP_MUTATION_PREVIEW_FILES]
    path_label = "、".join(visible_paths)
    if len(paths) > len(visible_paths):
        path_label += f" 等 {len(paths)} 个文件"
    return path_label, f"{action} <code>{html.escape(path_label[:240])}</code>"


def _format_mutation_completion(tool_name: str, path: str, *, failed: bool) -> str:
    action = "编辑" if tool_name == "edit" else "写入"
    marker = "⚠️" if failed else "✅"
    state = "失败" if failed else "完成"
    return f"{marker} <b>{action}{state}</b> <code>{html.escape(path[:240])}</code>"


class OmpBackend(Backend):
    name = "omp"
    pane_command_name = "omp"

    def __init__(self) -> None:
        self._runtime_metadata_cache: dict[Path, tuple[int, int, ProviderRuntimeMetadata]] = {}
        self._plan_mode_cache: dict[Path, tuple[int, int, PlanModeSnapshot | None]] = {}
        self._model_context_cache: dict[Path, tuple[int, int, dict[tuple[str, str], int]]] = {}
        self._pending_write_previews: dict[tuple[str, str], tuple[str, str]] = {}
        self._pending_mutation_labels: dict[tuple[str, str], tuple[str, str]] = {}

    @property
    def start_cmd(self) -> str:
        return _start_cmd()

    def _process_executable_name(self) -> str:
        executable = ProviderDiscovery.resolve_executable("omp")
        return Path(executable).name if executable else self.pane_command_name

    @property
    def running_command_names(self) -> frozenset[str]:
        return frozenset({self.pane_command_name, self._process_executable_name()})

    def is_running_command(self, command: str) -> bool:
        return command in self.running_command_names

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            name=self.name,
            supports_structured_transcript=True,
            supports_resume=True,
            supports_usage=True,
            supports_interactive_pickers=True,
            accepts_input_while_busy=True,
        )

    def interactive_commands(self) -> dict[str, str]:
        return {
            "/login": "管理 OMP provider 登录凭据。",
            "/model": "切换模型，无参数时打开模型 picker。",
            "/scoped-models": "配置当前会话的模型循环范围。",
            "/settings": "打开 OMP 设置界面。",
            "/statusline": "打开 OMP 自定义状态栏设置界面。",
            "/resume": "恢复历史会话并切换当前 provider session。",
            "/tree": "浏览当前会话树并选择继续位置。",
            "/trust": "保存当前项目的信任决定。",
            "/fork": "从历史 user message 创建新会话。",
            "/import": "导入 JSONL 并切换当前 provider session。",
        }

    @property
    def remote_tui_actions_allowed(self) -> bool:
        return False

    @property
    def requires_idle_for_control_commands(self) -> bool:
        return True

    @property
    def restart_via_clean_respawn(self) -> bool:
        return True

    @property
    def prefers_native_status_identity(self) -> bool:
        return True

    def format_remote_interaction_notice(self, binding: "Binding", interaction_label: str) -> str:
        return omp_ssh_interaction_notice(binding, interaction_label=interaction_label)

    def format_remote_access_notice(self, binding: "Binding", interaction_label: str) -> str:
        return omp_ssh_interaction_notice(
            binding, interaction_label=interaction_label, confirmed=False
        )

    bot_commands = [
        ("menu", "🎛 中文控制面板"),
        ("settings", "⚙️ 打开控制面板"),
        ("mention", "@ 群聊唤醒开关"),
        ("status", "ℹ️ OMP 状态"),
        ("info", "📊 累计 token (jsonl)"),
        ("whoami", "👤 我的 user_id / chat_id"),
        ("new", "🆕 开新会话"),
        ("resume", "🔄 恢复历史会话"),
        ("plan", "📝 计划模式"),
        ("esc", "⎋ 中断当前生成"),
        ("cc", "⌃C 取消/清空输入"),
        ("eof", "⌃D 退出 OMP"),
        ("tmuxstop", "💤 关闭 tmux，来消息再恢复"),
        ("screen", "📷 抓 tmux 屏幕"),
        ("restart", "🔄 重启 OMP"),
    ]

    def parse_terminal_status(self, pane: str) -> TerminalStatus | None:
        clean = strip_decorations(pane)
        lines = [line.rstrip() for line in clean.splitlines()]
        pair = find_omp_footer_pair(lines)
        if pair is None:
            return None

        top_index, bottom_index = pair
        top = _omp_footer_content(lines[top_index])
        bottom = _omp_footer_content(lines[bottom_index])
        footer = " • ".join(part for part in (top, bottom) if part)
        loader = omp_live_loader(clean)

        context_match = _OMP_CONTEXT_RE.search(footer)
        context_limit = context_used = None
        context_percent = None
        auto_compact = None
        if context_match is not None:
            context_limit = _scaled_number(context_match.group("limit"))
            percent = context_match.group("percent")
            if percent is not None:
                context_percent = float(percent)
                if context_limit:
                    context_used = round(context_percent / 100 * context_limit)
            auto_compact = bool(context_match.group("auto"))

        model_match = _OMP_ICON_MODEL_RE.search(footer)
        fallback_model_match = None if model_match else _OMP_LABELED_MODEL_RE.search(footer)
        effort_match = _OMP_ICON_EFFORT_RE.search(footer) or _OMP_EFFORT_RE.search(footer)
        icon_cwd_match = _OMP_ICON_CWD_RE.search(footer)
        icon_branch_match = _OMP_ICON_BRANCH_RE.search(footer)
        location_match = _OMP_LOCATION_RE.search(footer)
        label_match = _OMP_SESSION_LABEL_RE.search(footer)
        active_label_match = _OMP_ACTIVE_LABEL_RE.search(top)
        compact_label_match = _OMP_COMPACT_LABEL_RE.search(top)
        cost_match = _OMP_COST_RE.search(footer)
        session_tokens_match = _OMP_SESSION_TOKENS_RE.search(footer)
        cwd = (
            icon_cwd_match.group("cwd").strip()
            if icon_cwd_match
            else location_match.group("cwd")
            if location_match
            else None
        )
        git_branch = (
            icon_branch_match.group("branch").strip()
            if icon_branch_match
            else location_match.group("branch").strip()
            if location_match and location_match.group("branch")
            else None
        )
        return TerminalStatus(
            provider=model_match.group("provider").strip() if model_match else None,
            model=(
                model_match.group("model").strip()
                if model_match
                else fallback_model_match.group("model").strip()
                if fallback_model_match
                else None
            ),
            state=TerminalState.WORKING if loader else TerminalState.IDLE,
            label=loader or "ready",
            effort=_normalize_omp_effort(effort_match),
            cwd=cwd,
            git_branch=git_branch,
            session_name=(
                active_label_match.group("label").strip()
                if active_label_match
                else compact_label_match.group("label").strip()
                if compact_label_match
                else label_match.group("label").strip()
                if label_match
                else None
            ),
            session_total_tokens=(
                _scaled_number(session_tokens_match.group("value"))
                if session_tokens_match
                else None
            ),
            cost_usd=float(cost_match.group("value")) if cost_match else None,
            subscription=bool(cost_match.group("subscription")) if cost_match else None,
            context_used=context_used,
            context_limit=context_limit,
            context_percent=context_percent,
            auto_compact=auto_compact,
        )

    def find_active_jsonl(self, b: "Binding") -> Path | None:
        handoff = read_handoff(b.tmux_target, b.cwd)
        if handoff is not None:
            return handoff.transcript_path

        if b.transcript_path is None:
            return None
        transcript = Path(b.transcript_path)
        if read_session_header(transcript, b.cwd) is None:
            return None
        return transcript

    def session_identity(self, b: "Binding", transcript_path: Path) -> SessionIdentity:
        header = read_session_header(transcript_path, b.cwd)
        if header is None:
            raise ValueError(f"invalid OMP transcript identity: {transcript_path}")
        return SessionIdentity(
            provider=self.name,
            session_id=str(header["id"]),
            transcript_path=str(transcript_path),
            tmux_target=b.tmux_target,
            cwd=str(b.cwd.expanduser().resolve()),
        )

    def parse_event(self, line: str, provider_session_id: str | None = None) -> list[ProviderEvent]:
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            return []
        if not isinstance(row, dict):
            return []
        return self._parse_row(row, provider_session_id)

    def _parse_row(
        self, row: dict[str, Any], provider_session_id: str | None = None
    ) -> list[ProviderEvent]:
        row_type = row.get("type")
        if row_type == "compaction":
            try:
                tokens_before = int(row.get("tokensBefore", 0) or 0)
            except (TypeError, ValueError):
                tokens_before = 0
            return [
                self.provider_event(
                    row,
                    ProviderEventKind.LIFECYCLE_CHANGE,
                    (
                        "✅ <b>OMP 上下文压缩已完成</b>\n"
                        f"· 压缩前上下文 <code>{tokens_before:,}</code> tokens"
                    ),
                    provider_session_id=provider_session_id,
                    metadata={
                        "lifecycle": "compaction_end",
                        "tokens_before": tokens_before,
                    },
                )
            ]
        if row_type != "message":
            return []
        message = row.get("message")
        if not isinstance(message, dict):
            return []
        role = message.get("role")
        if role == "toolResult":
            tool_name = str(message.get("toolName") or "").lower()
            tool_call_id = message.get("toolCallId")
            cache_key = (
                (provider_session_id or "unknown", tool_call_id)
                if isinstance(tool_call_id, str) and tool_call_id
                else None
            )
            pending_mutation = (
                self._pending_mutation_labels.pop(cache_key, None)
                if cache_key is not None
                else None
            )
            pending_write = (
                self._pending_write_previews.pop(cache_key, None)
                if cache_key is not None and tool_name == "write"
                else None
            )
            failed = message.get("isError") is True
            if failed:
                if pending_mutation is None:
                    return []
                preview = _format_mutation_completion(
                    pending_mutation[0], pending_mutation[1], failed=True
                )
            else:
                preview = _format_edit_result_preview(message) if tool_name == "edit" else None
                if tool_name == "write" and pending_write is not None:
                    preview = _format_write_result_preview(*pending_write)
                if preview is None and pending_mutation is not None:
                    preview = _format_mutation_completion(
                        pending_mutation[0], pending_mutation[1], failed=False
                    )
            if preview is None:
                return []
            native_id = f"{tool_call_id}:mutation" if tool_call_id else row.get("id")
            return [
                self.provider_event(
                    row,
                    ProviderEventKind.TOOL_PROGRESS,
                    preview,
                    provider_session_id=provider_session_id,
                    native_id=str(native_id) if native_id else None,
                    metadata={
                        "tool_call_id": tool_call_id,
                        "tool_phase": "result",
                        "tool_name": tool_name,
                    },
                )
            ]
        if role != "assistant":
            return []

        tools: list[str] = []
        texts: list[str] = []
        plan_updates: list[tuple[str, str]] = []
        mutation_starts: list[tuple[str, str, str]] = []
        content = message.get("content")
        if not isinstance(content, list):
            content = []
        for block in content:
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if kind == "text":
                text = block.get("text")
                if isinstance(text, str) and text:
                    texts.append(html.escape(text))
            elif kind == "thinking":
                thinking = str(block.get("thinking") or "").strip()
                if thinking:
                    tools.append(
                        f"💭 <i>{html.escape(thinking[:300])}"
                        f"{'…' if len(thinking) > 300 else ''}</i>"
                    )
            elif kind == "toolCall":
                name = str(block.get("name") or "?")
                arguments = block.get("arguments")
                tool_call_id = block.get("id")
                mutation = (
                    _mutation_start(name, arguments)
                    if isinstance(tool_call_id, str) and tool_call_id
                    else None
                )
                if mutation is None:
                    tools.append(_format_tool(name, arguments))
                else:
                    path_label, start_text = mutation
                    key = (provider_session_id or "unknown", tool_call_id)
                    self._pending_mutation_labels[key] = (name.lower(), path_label)
                    mutation_starts.append((tool_call_id, name.lower(), start_text))
                    if len(self._pending_mutation_labels) > _OMP_PENDING_WRITE_LIMIT:
                        oldest = next(iter(self._pending_mutation_labels))
                        self._pending_mutation_labels.pop(oldest, None)
                if (
                    name.lower() == "write"
                    and isinstance(tool_call_id, str)
                    and tool_call_id
                    and isinstance(arguments, dict)
                ):
                    path = _safe_mutation_preview_path(arguments.get("path"))
                    write_content = arguments.get("content")
                    if path is not None and isinstance(write_content, str) and write_content:
                        key = (provider_session_id or "unknown", tool_call_id)
                        self._pending_write_previews[key] = (
                            path,
                            _bounded_mutation_preview(write_content),
                        )
                        if len(self._pending_write_previews) > _OMP_PENDING_WRITE_LIMIT:
                            oldest = next(iter(self._pending_write_previews))
                            self._pending_write_previews.pop(oldest, None)
                plan = plan_text_from_write_block(block)
                if plan is not None and isinstance(tool_call_id, str) and tool_call_id:
                    plan_updates.append((tool_call_id, plan))

        native_id = row.get("id") or message.get("responseId")
        events: list[ProviderEvent] = []
        if message.get("stopReason") == "error":
            error_message = str(message.get("errorMessage") or "OMP provider request failed")
            if is_user_abort_error(error_message):
                return []
            return [
                self.provider_event(
                    row,
                    ProviderEventKind.PROVIDER_ERROR,
                    f"⚠️ <b>OMP 请求失败</b>\n· {html.escape(error_message)}",
                    provider_session_id=provider_session_id,
                    native_id=f"{native_id}:error" if native_id else None,
                )
            ]
        if tools:
            events.append(
                self.provider_event(
                    row,
                    ProviderEventKind.TOOL_PROGRESS,
                    "\n".join(tools),
                    provider_session_id=provider_session_id,
                    native_id=f"{native_id}:tools" if native_id else None,
                )
            )
        for tool_call_id, tool_name, start_text in mutation_starts:
            events.append(
                self.provider_event(
                    row,
                    ProviderEventKind.TOOL_PROGRESS,
                    start_text,
                    provider_session_id=provider_session_id,
                    native_id=f"{tool_call_id}:start",
                    metadata={
                        "tool_call_id": tool_call_id,
                        "tool_phase": "start",
                        "tool_name": tool_name,
                    },
                )
            )
        for tool_call_id, plan in plan_updates:
            events.append(
                self.provider_event(
                    row,
                    ProviderEventKind.PLAN_UPDATE,
                    html.escape(plan),
                    provider_session_id=provider_session_id,
                    native_id=tool_call_id,
                )
            )
        if texts:
            events.append(
                self.provider_event(
                    row,
                    ProviderEventKind.FINAL_TEXT,
                    "\n".join(texts),
                    provider_session_id=provider_session_id,
                    native_id=f"{native_id}:text" if native_id else None,
                )
            )
        return events

    def provider_error_is_managed(self, b: "Binding") -> bool:
        """Whether the loaded OMP extension owns final error notification.

        Existing long-running OMP processes may not have reloaded the managed
        session-health reporter yet.  In that migration state transcript errors
        must retain the legacy immediate IM path instead of disappearing.
        """
        health = read_session_health(b.tmux_target, b.cwd)
        return bool(
            health is not None
            and health.session_id == b.provider_session_id
            and b.transcript_path is not None
            and health.transcript_path == Path(b.transcript_path)
        )

    def read_plan_mode(self, b: "Binding") -> PlanModeSnapshot | None:
        transcript = self.find_active_jsonl(b)
        if transcript is None:
            return None
        try:
            stat = transcript.stat()
        except OSError:
            return None
        cached = self._plan_mode_cache.get(transcript)
        if cached is not None and cached[:2] == (stat.st_size, stat.st_mtime_ns):
            return cached[2]
        snapshot = read_plan_mode_snapshot(transcript)
        self._plan_mode_cache[transcript] = (stat.st_size, stat.st_mtime_ns, snapshot)
        return snapshot

    def enrich_terminal_status(
        self, b: "Binding", status: TerminalStatus | None
    ) -> TerminalStatus | None:
        transcript = self.find_active_jsonl(b)
        session_file_size = None
        if transcript is not None:
            try:
                session_file_size = transcript.stat().st_size
            except OSError:
                pass

        snapshot = self.read_plan_mode(b)
        if status is None:
            if session_file_size is None and snapshot is None:
                return None
            metadata = self.current_runtime_metadata(b)
            return TerminalStatus(
                state=TerminalState.IDLE,
                provider=metadata.provider,
                model=metadata.model,
                effort=metadata.effort,
                session_name=metadata.session_name,
                session_file_size_bytes=session_file_size,
                session_total_tokens=metadata.session_total_tokens,
                input_tokens=metadata.input_tokens,
                output_tokens=metadata.output_tokens,
                cache_read_tokens=metadata.cache_read_tokens,
                cache_write_tokens=metadata.cache_write_tokens,
                cache_hit_rate=metadata.cache_hit_rate,
                cost_usd=metadata.cost_usd,
                extension_statuses=(snapshot.footer,) if snapshot is not None else (),
            )

        enriched = status
        if session_file_size is not None and status.session_file_size_bytes != session_file_size:
            enriched = replace(status, session_file_size_bytes=session_file_size)
        if snapshot is None:
            return enriched

        existing = tuple(enriched.extension_statuses)
        has_plan_status = any(
            re.search(
                r"(?:^|\s)(?:📝\s*)?plan(?:\s+(?:active|ready|saved|implementing|✓))?(?:\s|$|•)",
                item,
                re.I,
            )
            for item in existing
        )
        if has_plan_status:
            return enriched
        return replace(enriched, extension_statuses=(*existing, snapshot.footer))

    def render_extension_footer(self, b: "Binding") -> str:
        snapshot = self.read_plan_mode(b)
        return snapshot.widget if snapshot is not None else ""

    @staticmethod
    def _model_config_paths(transcript: Path) -> tuple[Path, ...]:
        candidates: list[Path] = []
        configured_root = os.getenv("PI_CODING_AGENT_DIR", "").strip()
        if configured_root:
            candidates.append(Path(configured_root).expanduser() / "models.yml")
        for parent in transcript.parents:
            if parent.name == "sessions":
                candidates.append(parent.parent / "models.yml")
                break
        candidates.append(Path.home() / ".omp" / "agent" / "models.yml")
        return tuple(dict.fromkeys(candidates))

    def _model_context_limit(
        self,
        transcript: Path,
        provider: str | None,
        model: str | None,
    ) -> int | None:
        if not model:
            return None
        model_key = model.lower()
        provider_key = provider.lower() if provider else None
        for config_path in self._model_config_paths(transcript):
            try:
                stat = config_path.stat()
            except OSError:
                continue
            cached = self._model_context_cache.get(config_path)
            if cached is None or cached[:2] != (stat.st_size, stat.st_mtime_ns):
                contexts: dict[tuple[str, str], int] = {}
                try:
                    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
                except (OSError, yaml.YAMLError, UnicodeError):
                    raw = {}
                providers = raw.get("providers") if isinstance(raw, dict) else None
                if isinstance(providers, dict):
                    for raw_provider, provider_config in providers.items():
                        models = (
                            provider_config.get("models")
                            if isinstance(provider_config, dict)
                            else None
                        )
                        if not isinstance(raw_provider, str) or not isinstance(models, list):
                            continue
                        for item in models:
                            if not isinstance(item, dict):
                                continue
                            raw_model = item.get("id")
                            context_window = item.get("contextWindow")
                            if (
                                isinstance(raw_model, str)
                                and type(context_window) is int
                                and context_window > 0
                            ):
                                contexts[(raw_provider.lower(), raw_model.lower())] = context_window
                self._model_context_cache[config_path] = (
                    stat.st_size,
                    stat.st_mtime_ns,
                    contexts,
                )
            else:
                contexts = cached[2]

            if provider_key is not None:
                exact = contexts.get((provider_key, model_key))
                if exact is not None:
                    return exact
            matches = {
                limit for (_, candidate), limit in contexts.items() if candidate == model_key
            }
            if len(matches) == 1:
                return matches.pop()
        return None

    def current_runtime_metadata(self, b: "Binding") -> ProviderRuntimeMetadata:
        transcript = self.find_active_jsonl(b)
        if transcript is None:
            return ProviderRuntimeMetadata()
        try:
            stat = transcript.stat()
        except OSError:
            return ProviderRuntimeMetadata()
        cached = self._runtime_metadata_cache.get(transcript)
        if cached is not None and cached[:2] == (stat.st_size, stat.st_mtime_ns):
            return cached[2]

        entries = read_jsonl_entries(transcript)
        branch = current_jsonl_branch_from_entries(entries)
        title_slot = header_title = None
        for row in entries:
            row_type = row.get("type")
            title = row.get("title")
            if row_type == "title" and title_slot is None:
                if isinstance(title, str) and title.strip():
                    title_slot = title.strip()
            elif row_type == "session":
                if isinstance(title, str) and title.strip():
                    header_title = title.strip()
                break
        session_name = title_slot or header_title

        canonical_model: tuple[str, str] | None = None
        fallback_provider = fallback_model = effort = None
        input_tokens = output_tokens = cache_read = cache_write = session_total_tokens = 0
        latest_cache_hit_rate = latest_context_used = None
        cost_usd = 0.0
        for row in branch:
            row_type = row.get("type")
            if row_type == "title_change":
                title = row.get("title")
                if isinstance(title, str) and title.strip():
                    session_name = title.strip()
                continue
            if row_type == "thinking_level_change":
                value = row.get("thinkingLevel")
                if isinstance(value, str) and value:
                    effort = value
                continue
            if row_type == "model_change":
                model_ref = _split_model_reference(row.get("model"))
                if model_ref is not None:
                    canonical_model = model_ref
                continue
            if row_type != "message":
                continue
            message = row.get("message")
            if not isinstance(message, dict) or message.get("role") != "assistant":
                continue
            assistant_provider = message.get("provider")
            assistant_model = message.get("model")
            if isinstance(assistant_provider, str) and assistant_provider:
                fallback_provider = assistant_provider
            if isinstance(assistant_model, str) and assistant_model:
                fallback_model = assistant_model
            usage = message.get("usage")
            if not isinstance(usage, dict):
                continue
            current_input = _usage_int(usage, "input")
            current_cache_read = _usage_int(usage, "cacheRead")
            current_cache_write = _usage_int(usage, "cacheWrite")
            current_output = _usage_int(usage, "output")
            orchestration = usage.get("orchestration")
            orchestration_input = _usage_int(usage, "orchestrationInput")
            orchestration_output = _usage_int(usage, "orchestrationOutput")
            if isinstance(orchestration, dict):
                orchestration_input = orchestration_input or _usage_int(orchestration, "input")
                orchestration_output = orchestration_output or _usage_int(orchestration, "output")
            session_total_tokens += (
                current_input
                + current_output
                + current_cache_write
                + orchestration_input
                + orchestration_output
            )
            input_tokens += current_input
            output_tokens += current_output
            cache_read += current_cache_read
            cache_write += current_cache_write
            cost_usd += _usage_cost(usage)
            current_context = _usage_int(usage, "totalTokens") or (
                current_input + current_output + current_cache_read + current_cache_write
            )
            if current_context:
                latest_context_used = current_context
            prompt_tokens = current_input + current_cache_read + current_cache_write
            if prompt_tokens:
                latest_cache_hit_rate = current_cache_read / prompt_tokens

        if canonical_model is not None:
            provider, model = canonical_model
        else:
            provider, model = fallback_provider, fallback_model
        context_limit = self._model_context_limit(transcript, provider, model)
        context_percent = (
            latest_context_used / context_limit * 100
            if latest_context_used is not None and context_limit
            else None
        )
        metadata = ProviderRuntimeMetadata(
            provider=provider,
            model=model,
            effort=effort,
            session_name=session_name,
            session_total_tokens=session_total_tokens or None,
            input_tokens=input_tokens or None,
            output_tokens=output_tokens or None,
            cache_read_tokens=cache_read or None,
            cache_write_tokens=cache_write or None,
            cache_hit_rate=latest_cache_hit_rate,
            cost_usd=cost_usd or None,
            context_used=latest_context_used,
            context_limit=context_limit,
            context_percent=context_percent,
        )
        self._runtime_metadata_cache[transcript] = (
            stat.st_size,
            stat.st_mtime_ns,
            metadata,
        )
        return metadata

    def current_model(self, b: "Binding") -> str | None:
        return self.current_runtime_metadata(b).model

    def current_effort(self, b: "Binding") -> str | None:
        return self.current_runtime_metadata(b).effort

    def format_status_footer(self, status: TerminalStatus | None) -> str | None:
        if status is None:
            return None
        parts: list[str] = []

        if status.context_limit is not None:
            percent = (
                f"{_format_omp_status_percent(status.context_percent)}%"
                if status.context_percent is not None
                else "?"
            )
            context_used = status.context_used
            if context_used is None and status.context_percent is not None:
                context_used = round(status.context_percent / 100 * status.context_limit)
            if context_used is not None:
                remaining = max(0, status.context_limit - context_used)
                parts.append(
                    f"上下文 {percent} "
                    f"({_format_omp_context_limit(context_used)}/"
                    f"{_format_omp_context_limit(status.context_limit)}，"
                    f"余 {_format_omp_context_limit(remaining)})"
                )
            else:
                parts.append(f"上下文 {percent}/{_format_omp_context_limit(status.context_limit)}")

        if status.session_total_tokens is not None:
            parts.append(f"会话累计 {_format_omp_tokens(status.session_total_tokens)} tok")
        if status.cache_hit_rate is not None:
            parts.append(f"缓存命中 {status.cache_hit_rate * 100:.1f}%")
        if status.auto_compact is not None:
            parts.append("自动压缩 开" if status.auto_compact else "自动压缩 关")

        if status.model:
            identity = status.model
            if status.provider:
                identity += f" ({status.provider})"
            parts.append(f"模型 {identity}")
        elif status.provider:
            parts.append(f"模型 {status.provider}")
        if status.effort:
            parts.append(f"思考 {status.effort}")
        if status.extension_statuses:
            parts.append(" ".join(status.extension_statuses))
        return " · ".join(parts) or None

    def find_tui_activity_fp(self, pane: str) -> str | None:
        status = self.parse_terminal_status(pane)
        if status and status.state == TerminalState.WORKING:
            return status.label
        return None

    async def ensure_running(self, b: "Binding") -> None:
        if not tmux_has_session(b.tmux_session):
            tmux_new_session(b.tmux_session, b.cwd)
            await asyncio.sleep(0.5)
        command = tmux_pane_command(b.tmux_target)
        if self.is_running_command(command):
            if not provider_tree_is_safe(b.tmux_target, self._process_executable_name()):
                raise RuntimeError(
                    f"OMP process tree in pane {b.tmux_target} contains a stopped or missing OMP; "
                    "refusing to inject input"
                )
            if read_handoff(b.tmux_target, b.cwd) is None:
                raise RuntimeError(
                    f"OMP pane {b.tmux_target} 缺少有效的受管会话身份 sidecar，已拒绝注入 IM；"
                    "请执行 /restart 或通过 Web 重新启动受管 OMP"
                )
            return
        if not self.can_start_from_command(command):
            raise RuntimeError(
                f"refusing to start OMP in pane {b.tmux_target} with foreground command {command!r}"
            )
        resume_pin = Path(b.transcript_path) if b.transcript_path is not None else None
        if resume_pin is not None and read_session_header(resume_pin, b.cwd) is None:
            raise RuntimeError(
                f"OMP transcript pin identity is invalid for pane {b.tmux_target}; "
                "pin preserved and launch refused"
            )
        launch_started_at = time.time()
        launched = await tmux_safe_launch(
            b.tmux_target,
            _start_cmd(resume_pin),
            allowed_shells=self.shell_command_names,
        )
        if not launched:
            raise RuntimeError(
                f"OMP launch aborted after foreground revalidation in pane {b.tmux_target}"
            )
        for _ in range(40):
            await asyncio.sleep(0.25)
            if not self.is_running_command(tmux_pane_command(b.tmux_target)):
                continue
            if not provider_tree_is_safe(b.tmux_target, self._process_executable_name()):
                continue
            handoff = read_handoff(b.tmux_target, b.cwd)
            if handoff is not None and handoff.updated_at > launch_started_at:
                return
        raise RuntimeError(
            f"OMP pane {b.tmux_target} 启动后未发布本次启动的有效受管会话身份 sidecar；"
            "transcript pin 保持不变，请执行 /restart 或通过 Web 重新启动受管 OMP"
        )

    async def recover_unhealthy_pane(self, b: "Binding") -> bool:
        """Discard only an unsafe OMP process tree, then resume its pinned session."""
        if self.is_running_command(tmux_pane_command(b.tmux_target)) and provider_tree_is_safe(
            b.tmux_target, self._process_executable_name()
        ):
            return False
        if not tmux_respawn_pane(b.tmux_target, b.cwd):
            return False
        await asyncio.sleep(0.25)
        await self.ensure_running(b)
        return True

    async def reconcile_session_identity(self, b: "Binding") -> bool:
        """Adopt only a provider-authored identity refreshed after the pending command."""
        handoff_after = b.pending_session_handoff_after
        if handoff_after is None:
            return False
        for _ in range(40):
            handoff = read_handoff(b.tmux_target, b.cwd)
            if handoff is not None and handoff.updated_at > handoff_after:
                b.provider_session_id = handoff.session_id
                b.transcript_path = handoff.transcript_path
                b.last_session_id = handoff.session_id
                b.pending_session_handoff_after = None
                return True
            await asyncio.sleep(0.25)
        return False

    def interactive_session_handoff_commands(self) -> frozenset[str]:
        return frozenset({"/resume", "/import"})

    def command_opts(self) -> dict[str, CmdOpts]:
        return {
            "/new": CmdOpts(
                init_delay=0.4,
                poll=0.3,
                max_iters=20,
                expect_new_session=True,
                defer_new_session_persistence=True,
                done_pattern=re.compile(r"[✓✔]\s*New session started", re.I),
                fallback_summary="✅ <b>OMP 新会话已启动</b>\n· 新会话将在首条回复落盘后绑定",
            ),
            "/fork": CmdOpts(
                init_delay=0.4,
                poll=0.3,
                max_iters=20,
                expect_new_session=True,
                expect_session_handoff=True,
                fallback_summary="✅ <b>OMP fork 已切换到新会话</b>",
                failure_summary="⚠️ <b>OMP fork 未确认新会话身份</b>",
            ),
            "/compact": CmdOpts(
                init_delay=1.0,
                poll=0.5,
                max_iters=240,
                expect_session_handoff=True,
                expect_compact_done=True,
                notice="⏳ OMP 压缩上下文中…",
                fallback_summary="✅ <b>OMP 上下文压缩已结束</b>",
                failure_summary=(
                    "⚠️ <b>OMP 未生成压缩记录或切换会话</b>\n"
                    "· 当前会话可能太小、压缩被取消或 provider 返回错误；请用 /screen 查看 TUI"
                ),
            ),
            "/clear": CmdOpts(
                init_delay=0.4,
                poll=0.3,
                max_iters=12,
                fallback_summary="✅ <b>OMP 当前会话上下文已清空</b>",
            ),
            "/fresh": CmdOpts(
                init_delay=0.4,
                poll=0.3,
                max_iters=12,
                fallback_summary="✅ <b>OMP provider 状态已刷新</b>",
            ),
        }

    def command_aliases(self) -> dict[str, str]:
        return {}

    def compact_metadata_since(self, jsonl_path: Path | None, since_byte: int = 0) -> dict | None:
        if jsonl_path is None:
            return None
        try:
            with jsonl_path.open("rb") as stream:
                stream.seek(max(0, since_byte))
                chunk = stream.read().decode("utf-8", errors="replace")
        except OSError:
            return None
        for line in reversed(chunk.splitlines()):
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("type") != "compaction":
                continue
            try:
                pre_tokens = int(row.get("tokensBefore", 0) or 0)
            except (TypeError, ValueError):
                pre_tokens = 0
            trigger = row.get("trigger")
            return {
                "preTokens": pre_tokens or None,
                "postTokens": None,
                "durationMs": None,
                "trigger": trigger if isinstance(trigger, str) and trigger else "manual",
            }
        return None

    def read_tasks(self, b: "Binding") -> list[dict[str, str]]:
        transcript = self.find_active_jsonl(b)
        if transcript is None:
            return []
        for row in reversed(current_jsonl_branch(transcript)):
            if row.get("type") == "custom" and row.get("customType") == "user_todo_edit":
                data = row.get("data")
                phases = data.get("phases") if isinstance(data, dict) else None
                return _normalize_todo_phases(phases) or []
            if row.get("type") != "message":
                continue
            message = row.get("message")
            if (
                not isinstance(message, dict)
                or message.get("role") != "toolResult"
                or message.get("toolName") != "todo"
                or message.get("isError") is True
            ):
                continue
            details = message.get("details")
            phases = details.get("phases") if isinstance(details, dict) else None
            return _normalize_todo_phases(phases) or []
        return []

    def estimated_compaction_seconds(self, b: "Binding") -> int:
        transcript = self.find_active_jsonl(b)
        if transcript is None:
            return 180
        durations: list[float] = []
        previous_timestamp: float | None = None
        try:
            rows = transcript.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return 180
        for line in rows:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            timestamp = _omp_timestamp(row.get("timestamp"))
            if row.get("type") == "compaction" and timestamp is not None:
                if previous_timestamp is not None:
                    duration = timestamp - previous_timestamp
                    if 20 <= duration <= 600:
                        durations.append(duration)
            if timestamp is not None and row.get("type") in {"message", "compaction"}:
                previous_timestamp = timestamp
        if not durations:
            return 180
        recent = sorted(durations[-5:])
        return max(30, min(360, round(recent[len(recent) // 2])))

    def aggregate_usage(self, jsonl_path: Path, last_n: int = 200) -> dict | None:
        branch = current_jsonl_branch(jsonl_path)
        total_input = total_output = cache_read = cache_write = count = 0
        last_ts = fallback_model = None
        canonical_model = None
        assistant_rows = 0
        for row in reversed(branch):
            if row.get("type") == "model_change" and canonical_model is None:
                model_ref = _split_model_reference(row.get("model"))
                if model_ref is not None:
                    canonical_model = model_ref[1]
                continue
            if row.get("type") != "message" or assistant_rows >= last_n:
                continue
            message = row.get("message")
            if not isinstance(message, dict) or message.get("role") != "assistant":
                continue
            assistant_rows += 1
            usage = message.get("usage")
            if not isinstance(usage, dict):
                continue
            total_input += _usage_int(usage, "input")
            total_output += _usage_int(usage, "output")
            cache_read += _usage_int(usage, "cacheRead")
            cache_write += _usage_int(usage, "cacheWrite")
            count += 1
            last_ts = last_ts or row.get("timestamp")
            model = message.get("model")
            if fallback_model is None and isinstance(model, str) and model:
                fallback_model = model
        if not any((total_input, total_output, cache_read, cache_write)):
            return None
        prompt_total = total_input + cache_read + cache_write
        return {
            "count": count,
            "input": total_input,
            "output": total_output,
            "cache_create": cache_write,
            "cache_read": cache_read,
            "cache_hit_rate": cache_read / prompt_total if prompt_total else 0,
            "last_ts": last_ts,
            "model": canonical_model or fallback_model or "omp",
        }
