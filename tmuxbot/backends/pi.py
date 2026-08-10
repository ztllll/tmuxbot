"""Pi interactive TUI backend.

Pi remains a real tmux TUI.  This adapter only knows how to launch/resume Pi,
find its local session JSONL, normalize transcript rows, and read terminal state.
It never uses Pi print, RPC, SDK, or server modes.
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
import shlex
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

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
from tmuxbot.runtime.pi_handoff import read_handoff
from tmuxbot.runtime.route_health import provider_session_file, provider_tree_is_safe
from tmuxbot.tmux import (
    tmux_capture,
    tmux_has_session,
    tmux_new_session,
    tmux_native_exit,
    tmux_pane_command,
    tmux_respawn_pane,
    tmux_safe_launch,
    tmux_send_text,
)
from tmuxbot.utils import strip_decorations

if TYPE_CHECKING:
    from tmuxbot.state import Binding

log = logging.getLogger("tmuxbot")

PI_SESSIONS_DIR = Path.home() / ".pi" / "agent" / "sessions"
# Pi's live status indicator always starts with its animated braille spinner.
# Requiring it prevents an assistant/user transcript line containing e.g.
# ``Working...`` from being mistaken for current TUI activity.
_PI_WORKING_RE = re.compile(
    r"^\s*[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏]\s+"
    r"(?:Working|Compacting context|Auto-compacting|Summarizing branch|Retrying)\.\.\.",
    re.I | re.M,
)
_PI_MODEL_RE = re.compile(
    r"^(?:(?:\((?P<provider>[^()\n]+)\)\s+))?"
    r"(?P<model>[A-Za-z0-9][A-Za-z0-9_.:/-]*)"
    r"(?:\s*[•·]\s*(?P<effort>off|minimal|low|medium|high|xhigh|max|thinking off))?$",
    re.I,
)
_PI_TRUNCATED_MODEL_RE = re.compile(
    r"^(?:(?:\((?P<provider>[^()\n]+)\)\s+))?"
    r"(?P<model>[A-Za-z0-9][A-Za-z0-9_.:/-]*)\s*[•·]?\s*$",
    re.I,
)
_PI_CONTEXT_RE = re.compile(
    r"(?:(?P<percent>\d+(?:\.\d+)?)%|\?)\s*/\s*"
    r"(?P<limit>\d+(?:\.\d+)?[kKmM]?)\s*(?P<auto>\(auto\))?"
)
_PI_USAGE_RE = re.compile(r"(?:^|\s)(?P<label>[↑↓RW])(?P<value>\d+(?:\.\d+)?[kKmM]?)")
_PI_CACHE_HIT_RE = re.compile(r"(?:^|\s)CH(?P<value>\d+(?:\.\d+)?)%")
_PI_COST_RE = re.compile(
    r"(?:^|\s)\$(?P<value>\d+(?:\.\d+)?)(?P<subscription>\s+\(sub\))?"
)
_PI_STATUSLINE_RE = re.compile(r"🪟\s*ctx\s+", re.M)
_PI_STATUSLINE_PROVIDER_RE = re.compile(r"🔌\s*(?P<provider>\S+)")
_PI_STATUSLINE_MODEL_RE = re.compile(
    r"🤖\s*(?P<model>.+?)(?=\s+🧠|\s+📁|\s+🌿|\s+💭|\s+⚙|\s+🪟|\s+🔢|\s+📦|\s+💸|\s+🕒|\s+🔁|\ue0b4|$)"
)
_PI_STATUSLINE_EFFORT_RE = re.compile(
    r"🧠\s*(?P<effort>off|minimal|low|medium|high|xhigh|max)\b", re.I
)
_PI_STATUSLINE_CWD_RE = re.compile(
    r"📁\s*(?P<cwd>.+?)(?=\s+🌿|\s+💭|\s+⚙|\s+🪟|\s+🔢|\s+📦|\s+💸|\s+🕒|\s+🔁|\ue0b4|$)"
)
_PI_STATUSLINE_BRANCH_RE = re.compile(
    r"🌿\s*(?P<branch>.+?)(?=\s+💭|\s+⚙|\s+🪟|\s+🔢|\s+📦|\s+💸|\s+🕒|\s+🔁|\ue0b4|$)"
)
_PI_STATUSLINE_CONTEXT_RE = re.compile(
    r"🪟\s*ctx\s*(?:(?P<percent>\d+(?:\.\d+)?)%|\?)\s*/\s*"
    r"(?P<limit>\d+(?:\.\d+)?[kKmM]?)"
)
_PI_STATUSLINE_TOKENS_RE = re.compile(
    r"🔢\s*(?:tok\s+0|↑(?P<input>\d+(?:\.\d+)?[kKmM]?)\s+"
    r"↓(?P<output>\d+(?:\.\d+)?[kKmM]?))"
)
_PI_STATUSLINE_CACHE_RE = re.compile(
    r"📦\s*(?P<cache>.+?)(?=\s+💸|\s+🕒|\s+🔁|\ue0b4|$)"
)
_PI_STATUSLINE_COST_RE = re.compile(
    r"💸\s*\$(?P<value>\d+(?:\.\d+)?)(?P<subscription>\s+\(sub\))?"
)


def _pi_bin() -> str:
    return os.getenv("PI_BIN", "pi")


def _start_cmd(session_id: str | None = None) -> str:
    arguments = [_pi_bin(), "--approve"]
    if session_id:
        arguments.extend(("--session", session_id))
    return " ".join(shlex.quote(argument) for argument in arguments)


def encode_pi_cwd(cwd: Path) -> str:
    """Mirror Pi SessionManager's ``--<cwd with /, \\, : as ->--`` encoding."""
    resolved = str(cwd.expanduser().resolve())
    body = resolved.lstrip("/\\")
    body = re.sub(r"[/\\:]", "-", body)
    return f"--{body}--"


def _pi_timestamp(value: Any) -> float | None:
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


def _format_pi_tokens(count: int) -> str:
    if count < 1_000:
        return str(count)
    if count < 10_000:
        return f"{count / 1_000:.1f}k"
    if count < 1_000_000:
        return f"{round(count / 1_000)}k"
    if count < 10_000_000:
        return f"{count / 1_000_000:.1f}M"
    return f"{round(count / 1_000_000)}M"


def _parse_pi_location_line(line: str) -> tuple[str | None, str | None, str | None]:
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


def _usage_tokens(usage: Any) -> int:
    if not isinstance(usage, dict):
        return 0
    total = usage.get("totalTokens")
    if total is not None:
        try:
            return int(total or 0)
        except (TypeError, ValueError):
            pass
    result = 0
    for key in ("input", "output", "cacheRead", "cacheWrite"):
        try:
            result += int(usage.get(key, 0) or 0)
        except (TypeError, ValueError):
            continue
    return result


def _retained_tail_tokens(value: Any) -> int | None:
    if not isinstance(value, list):
        return None
    total = 0
    found = False
    for message in value:
        if not isinstance(message, dict):
            continue
        usage = message.get("usage")
        if not isinstance(usage, dict):
            continue
        found = True
        total += _usage_tokens(usage)
    return total if found else 0


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


def _session_header(path: Path) -> dict[str, Any] | None:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as stream:
            for _ in range(32):
                line = stream.readline()
                if not line:
                    return None
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if row.get("type") == "session":
                    return row
    except OSError:
        return None
    return None


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


class PiBackend(Backend):
    name = "pi"
    pane_command_name = "pi"

    def __init__(self) -> None:
        self._runtime_metadata_cache: dict[
            Path, tuple[int, int, ProviderRuntimeMetadata]
        ] = {}

    @property
    def start_cmd(self) -> str:
        return _start_cmd()

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

    bot_commands = [
        ("menu", "🎛 中文控制面板"),
        ("settings", "⚙️ 打开控制面板"),
        ("mention", "@ 群聊唤醒开关"),
        ("status", "ℹ️ Pi 状态"),
        ("info", "📊 累计 token (jsonl)"),
        ("whoami", "👤 我的 user_id / chat_id"),
        ("new", "🆕 开新会话"),
        ("resume", "🔄 恢复历史会话"),
        ("esc", "⎋ 中断当前生成"),
        ("cc", "⌃C 取消/清空输入"),
        ("eof", "⌃D 退出 Pi"),
        ("tmuxstop", "💤 关闭 tmux，来消息再恢复"),
        ("screen", "📷 抓 tmux 屏幕"),
        ("restart", "🔄 重启 Pi"),
    ]

    def parse_terminal_status(self, pane: str) -> TerminalStatus | None:
        clean = strip_decorations(pane)
        if not clean.strip():
            return None
        lines = [line.rstrip() for line in clean.splitlines()]
        custom = self._parse_custom_statusline(lines, clean)
        if custom is not None:
            return custom
        working = _PI_WORKING_RE.search(clean)
        model_match = None
        stats_line = ""
        cwd = git_branch = session_name = None
        footer_index = None
        extension_statuses: list[str] = []
        for index in range(len(lines) - 1, -1, -1):
            stripped = lines[index].strip()
            context_candidate = _PI_CONTEXT_RE.search(stripped)
            if context_candidate is None:
                continue
            model_text = stripped[context_candidate.end() :].strip()
            candidate = _PI_MODEL_RE.fullmatch(model_text)
            if candidate is None:
                candidate = _PI_TRUNCATED_MODEL_RE.fullmatch(model_text)
            if candidate is None:
                continue
            model_match = candidate
            stats_line = stripped[: context_candidate.end()].rstrip()
            footer_index = index
            if index + 1 < len(lines):
                extension_statuses = [
                    line.strip() for line in lines[index + 1 :] if line.strip()
                ]
            break
        if footer_index is not None:
            for index in range(footer_index - 1, -1, -1):
                cwd, git_branch, session_name = _parse_pi_location_line(lines[index])
                if cwd is not None:
                    break

        context_match = _PI_CONTEXT_RE.search(stats_line)
        context_limit = context_used = None
        context_percent = None
        auto_compact = None
        if context_match:
            context_limit = _scaled_number(context_match.group("limit"))
            if context_match.group("percent") is not None:
                context_percent = float(context_match.group("percent"))
                if context_limit:
                    context_used = round(context_percent / 100 * context_limit)
            auto_compact = bool(context_match.group("auto"))

        usage_values: dict[str, int] = {}
        for match in _PI_USAGE_RE.finditer(stats_line):
            usage_values[match.group("label")] = _scaled_number(match.group("value"))
        cache_hit_match = _PI_CACHE_HIT_RE.search(stats_line)
        cost_match = _PI_COST_RE.search(stats_line)
        effort = model = provider = None
        if model_match:
            provider = model_match.group("provider")
            model = model_match.group("model")
            raw_effort = model_match.groupdict().get("effort")
            effort = raw_effort.lower().replace("thinking ", "") if raw_effort else None
        return TerminalStatus(
            state=TerminalState.WORKING if working else TerminalState.IDLE,
            label=working.group(0).strip() if working else "ready",
            provider=provider,
            model=model,
            effort=effort,
            cwd=cwd,
            git_branch=git_branch,
            session_name=session_name,
            input_tokens=usage_values.get("↑"),
            output_tokens=usage_values.get("↓"),
            cache_read_tokens=usage_values.get("R"),
            cache_write_tokens=usage_values.get("W"),
            cache_hit_rate=(
                float(cache_hit_match.group("value")) / 100
                if cache_hit_match is not None
                else None
            ),
            cost_usd=float(cost_match.group("value")) if cost_match is not None else None,
            subscription=(
                bool(cost_match.group("subscription")) if cost_match is not None else None
            ),
            extension_statuses=tuple(extension_statuses),
            context_used=context_used,
            context_limit=context_limit,
            context_percent=context_percent,
            auto_compact=auto_compact,
        )

    def _parse_custom_statusline(
        self, lines: list[str], clean: str
    ) -> TerminalStatus | None:
        index = next(
            (
                current
                for current in range(len(lines) - 1, -1, -1)
                if _PI_STATUSLINE_RE.search(lines[current])
            ),
            None,
        )
        if index is None:
            return None
        line = lines[index]
        working = _PI_WORKING_RE.search(clean)
        provider_match = _PI_STATUSLINE_PROVIDER_RE.search(line)
        model_match = _PI_STATUSLINE_MODEL_RE.search(line)
        effort_match = _PI_STATUSLINE_EFFORT_RE.search(line)
        cwd_match = _PI_STATUSLINE_CWD_RE.search(line)
        branch_match = _PI_STATUSLINE_BRANCH_RE.search(line)
        context_match = _PI_STATUSLINE_CONTEXT_RE.search(line)
        tokens_match = _PI_STATUSLINE_TOKENS_RE.search(line)
        cache_match = _PI_STATUSLINE_CACHE_RE.search(line)
        cost_match = _PI_STATUSLINE_COST_RE.search(line)

        context_limit = context_used = None
        context_percent = None
        if context_match:
            context_limit = _scaled_number(context_match.group("limit"))
            if context_match.group("percent") is not None:
                context_percent = float(context_match.group("percent"))
                if context_limit:
                    context_used = round(context_percent / 100 * context_limit)

        cache_read = cache_write = None
        cache_hit_rate = None
        if cache_match:
            cache_text = cache_match.group("cache")
            read_match = re.search(r"(?:^|\s)R(\d+(?:\.\d+)?[kKmM]?)", cache_text)
            write_match = re.search(r"(?:^|\s)W(\d+(?:\.\d+)?[kKmM]?)", cache_text)
            hit_match = re.search(r"(?:^|\s)CH(\d+(?:\.\d+)?)%", cache_text)
            cache_read = _scaled_number(read_match.group(1)) if read_match else None
            cache_write = _scaled_number(write_match.group(1)) if write_match else None
            cache_hit_rate = float(hit_match.group(1)) / 100 if hit_match else None

        # Pi renders extension-owned status below its powerline footer.  These
        # lines belong to the TUI status bar as a whole, not just to Pi's JSONL
        # indicator; preserve them verbatim so IM mirrors every extension.
        extension_statuses = tuple(item.strip() for item in lines[index + 1 :] if item.strip())
        return TerminalStatus(
            state=TerminalState.WORKING if working else TerminalState.IDLE,
            label=working.group(0).strip() if working else "ready",
            provider=provider_match.group("provider") if provider_match else None,
            model=(
                re.sub(r"^gpt\s+", "gpt-", model_match.group("model").strip())
                if model_match
                else None
            ),
            effort=effort_match.group("effort").lower() if effort_match else None,
            cwd=cwd_match.group("cwd").strip() if cwd_match else None,
            git_branch=(
                branch_match.group("branch").strip()
                if branch_match and branch_match.group("branch").strip() != "no-git"
                else None
            ),
            input_tokens=(
                _scaled_number(tokens_match.group("input"))
                if tokens_match and tokens_match.group("input")
                else 0 if tokens_match else None
            ),
            output_tokens=(
                _scaled_number(tokens_match.group("output"))
                if tokens_match and tokens_match.group("output")
                else 0 if tokens_match else None
            ),
            cache_read_tokens=cache_read,
            cache_write_tokens=cache_write,
            cache_hit_rate=cache_hit_rate,
            cost_usd=float(cost_match.group("value")) if cost_match else None,
            subscription=bool(cost_match.group("subscription")) if cost_match else None,
            extension_statuses=extension_statuses,
            context_used=context_used,
            context_limit=context_limit,
            context_percent=context_percent,
            auto_compact=None,
        )

    def find_active_jsonl(self, b: "Binding") -> Path | None:
        directory = PI_SESSIONS_DIR / encode_pi_cwd(b.cwd)
        if not directory.is_dir():
            return None
        target_cwd = str(b.cwd.expanduser().resolve())

        # The managed Pi extension records a session switch atomically.  It is
        # route-targeted rather than mtime-based, so another pane sharing this
        # cwd cannot be adopted accidentally.  Validate the transcript header
        # again here because the record itself is only a handoff hint.
        handoff = read_handoff(b.tmux_target, b.cwd)
        if handoff is not None:
            header = _session_header(handoff.transcript_path)
            if header and str(header.get("id") or "") == handoff.session_id:
                return handoff.transcript_path

        # Future Pi versions may export the exact current transcript on their
        # process environment.  Treat it as a second authoritative hint, not
        # as a fallback to newest-file guessing.
        live_path = provider_session_file(b.tmux_target, "pi")
        if live_path is not None:
            header = _session_header(live_path)
            try:
                live_cwd = str(Path(str((header or {}).get("cwd") or "")).expanduser().resolve())
            except OSError:
                live_cwd = ""
            if header and live_cwd == target_cwd:
                return live_path

        candidates: list[Path] = []
        handoff_candidates: list[Path] = []
        for path in directory.glob("*.jsonl"):
            header = _session_header(path)
            if not header:
                continue
            try:
                actual_cwd = str(Path(str(header.get("cwd") or "")).expanduser().resolve())
            except OSError:
                continue
            if actual_cwd != target_cwd:
                continue
            session_id = str(header.get("id") or path.stem)
            if (
                b.transcript_path
                and Path(b.transcript_path) == path
                and b.pending_session_handoff_after is None
            ):
                if not b.provider_session_id or b.provider_session_id == session_id:
                    return path
            if b.provider_session_id and b.provider_session_id == session_id:
                pinned = path
            else:
                pinned = None
            candidates.append(path)
            if (
                b.pending_session_handoff_after is not None
                and session_id != b.provider_session_id
                and path.stat().st_mtime >= b.pending_session_handoff_after
            ):
                handoff_candidates.append(path)
            if pinned is not None and b.pending_session_handoff_after is None:
                return pinned
        if handoff_candidates:
            return max(handoff_candidates, key=lambda path: path.stat().st_mtime)
        return max(candidates, key=lambda path: path.stat().st_mtime, default=None)

    def session_identity(self, b: "Binding", transcript_path: Path) -> SessionIdentity:
        header = _session_header(transcript_path) or {}
        return SessionIdentity(
            provider=self.name,
            session_id=str(header.get("id") or transcript_path.stem),
            transcript_path=str(transcript_path),
            tmux_target=b.tmux_target,
            cwd=str(b.cwd),
        )

    def parse_event(
        self, line: str, provider_session_id: str | None = None
    ) -> list[ProviderEvent]:
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            return []
        row_type = row.get("type")
        if row_type == "compaction":
            tokens_before = int(row.get("tokensBefore", 0) or 0)
            usage = row.get("usage") or {}
            return [
                self.provider_event(
                    row,
                    ProviderEventKind.LIFECYCLE_CHANGE,
                    (
                        "✅ <b>Pi 自动压缩已完成</b>\n"
                        f"· 压缩前上下文 <code>{tokens_before:,}</code> tokens\n"
                        f"· 摘要用量 <code>{int(usage.get('totalTokens', 0) or 0):,}</code> tokens"
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
        message = row.get("message") or {}
        if message.get("role") != "assistant":
            return []
        tools: list[str] = []
        texts: list[str] = []
        for block in message.get("content") or []:
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if kind == "text":
                text = str(block.get("text") or "")
                if text:
                    texts.append(html.escape(text))
            elif kind == "thinking":
                thinking = str(block.get("thinking") or "").strip()
                if thinking:
                    tools.append(
                        f"💭 <i>{html.escape(thinking[:300])}"
                        f"{'…' if len(thinking) > 300 else ''}</i>"
                    )
            elif kind == "toolCall":
                tools.append(
                    _format_tool(
                        str(block.get("name") or "?"),
                        block.get("arguments") or {},
                    )
                )
        native_id = row.get("id") or message.get("responseId")
        events: list[ProviderEvent] = []
        if message.get("stopReason") == "error":
            error_message = str(message.get("errorMessage") or "Pi provider request failed")
            events.append(
                self.provider_event(
                    row,
                    ProviderEventKind.PROVIDER_ERROR,
                    f"⚠️ <b>Pi 请求失败</b>\n· {html.escape(error_message)}",
                    provider_session_id=provider_session_id,
                    native_id=f"{native_id}:error" if native_id else None,
                )
            )
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
        provider = model = effort = session_name = None
        input_tokens = output_tokens = cache_read = cache_write = 0
        latest_cache_hit_rate = None
        cost_usd = 0.0
        try:
            rows = transcript.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return ProviderRuntimeMetadata()
        for line in rows:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            row_type = row.get("type")
            if row_type == "session_info":
                session_name = str(row.get("name") or "").strip() or None
                continue
            if row_type == "thinking_level_change":
                effort = row.get("thinkingLevel") or effort
                continue
            if row_type == "model_change":
                provider = row.get("provider") or provider
                model = row.get("modelId") or model
                continue
            usage = None
            update_cache_hit_rate = False
            if row_type == "message":
                message = row.get("message") or {}
                if message.get("role") == "assistant":
                    provider = message.get("provider") or provider
                    model = message.get("responseModel") or message.get("model") or model
                    usage = message.get("usage")
                    update_cache_hit_rate = True
                elif message.get("role") == "toolResult":
                    usage = message.get("usage")
            elif row_type in {"branch_summary", "compaction"}:
                usage = row.get("usage")
            if not isinstance(usage, dict):
                continue
            current_input = int(usage.get("input", 0) or 0)
            current_cache_read = int(usage.get("cacheRead", 0) or 0)
            current_cache_write = int(usage.get("cacheWrite", 0) or 0)
            input_tokens += current_input
            output_tokens += int(usage.get("output", 0) or 0)
            cache_read += current_cache_read
            cache_write += current_cache_write
            cost_usd += _usage_cost(usage)
            prompt_tokens = current_input + current_cache_read + current_cache_write
            if update_cache_hit_rate and prompt_tokens:
                latest_cache_hit_rate = current_cache_read / prompt_tokens
        metadata = ProviderRuntimeMetadata(
            provider=str(provider) if provider else None,
            model=str(model) if model else None,
            effort=str(effort) if effort else None,
            session_name=session_name,
            input_tokens=input_tokens or None,
            output_tokens=output_tokens or None,
            cache_read_tokens=cache_read or None,
            cache_write_tokens=cache_write or None,
            cache_hit_rate=latest_cache_hit_rate,
            cost_usd=cost_usd or None,
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
        if status.provider:
            parts.append(f"🔌 {status.provider}")
        if status.model:
            parts.append(f"🤖 {status.model}")
        if status.effort:
            parts.append(f"🧠 {status.effort}")

        tokens: list[str] = []
        if status.input_tokens is not None:
            tokens.append(f"↑{_format_pi_tokens(status.input_tokens)}")
        if status.output_tokens is not None:
            tokens.append(f"↓{_format_pi_tokens(status.output_tokens)}")
        if tokens:
            parts.append(f"🔢 {' '.join(tokens)}")

        cache: list[str] = []
        if status.cache_read_tokens is not None:
            cache.append(f"R{_format_pi_tokens(status.cache_read_tokens)}")
        if status.cache_write_tokens is not None:
            cache.append(f"W{_format_pi_tokens(status.cache_write_tokens)}")
        if status.cache_hit_rate is not None:
            cache.append(f"CH{status.cache_hit_rate * 100:.1f}%")
        if cache:
            parts.append(f"📦 {' '.join(cache)}")
        if status.cost_usd is not None or status.subscription:
            cost = status.cost_usd or 0.0
            parts.append(f"💸 ${cost:.3f}{' (sub)' if status.subscription else ''}")

        if status.context_limit is not None:
            context = "?"
            if status.context_used is not None:
                context = _format_pi_tokens(status.context_used)
            context += f"/{_format_pi_tokens(status.context_limit)}"
            details: list[str] = []
            if status.context_percent is not None:
                details.append(f"{status.context_percent:.1f}%")
            if status.auto_compact:
                details.append("auto")
            if details:
                context += f" ({', '.join(details)})"
            parts.append(f"🪟 {context}")

        if status.cwd:
            parts.append(f"📁 {status.cwd}")
        if status.git_branch:
            parts.append(f"🌿 {status.git_branch}")
        if status.session_name:
            parts.append(f"🏷 {status.session_name}")
        if status.extension_statuses:
            parts.append(" ".join(status.extension_statuses))
        if status.state != TerminalState.IDLE:
            state = status.label or status.state.value
            if status.duration_seconds is not None:
                state += f" {self._format_duration(status.duration_seconds)}"
            parts.append(state)
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
            # A foreground executable alone is not proof that the pane can
            # accept IM input.  A stopped sibling Pi can retain the terminal
            # process group after an interrupted /new or extension failure.
            # Refuse injection until an operator/recovery path replaces that
            # ambiguous process tree.
            if provider_tree_is_safe(b.tmux_target, "pi"):
                return
            raise RuntimeError(
                f"Pi process tree in pane {b.tmux_target} contains a stopped or missing Pi; "
                "refusing to inject input"
            )
        if not self.can_start_from_command(command):
            raise RuntimeError(
                f"refusing to start Pi in pane {b.tmux_target} "
                f"with foreground command {command!r}"
            )
        launched = await tmux_safe_launch(
            b.tmux_target,
            _start_cmd(b.provider_session_id or b.last_session_id),
            allowed_shells=self.shell_command_names,
        )
        if not launched:
            raise RuntimeError(
                f"Pi launch aborted after foreground revalidation in pane {b.tmux_target}"
            )
        for _ in range(40):
            await asyncio.sleep(0.25)
            if self.is_running_command(tmux_pane_command(b.tmux_target)):
                try:
                    pane = tmux_capture(b.tmux_target, 12)
                except Exception:
                    pane = ""
                clean = strip_decorations(pane)
                status = self.parse_terminal_status(pane)
                if _PI_WORKING_RE.search(clean) or (
                    status is not None
                    and (
                        status.model is not None
                        or status.context_limit is not None
                        or bool(status.extension_statuses)
                    )
                ):
                    return
        raise RuntimeError(f"Pi TUI did not become ready in pane {b.tmux_target}")

    async def recover_unhealthy_pane(self, b: "Binding") -> bool:
        """Discard only an unsafe Pi process tree, then resume its pinned session."""
        if tmux_pane_command(b.tmux_target) == "pi" and provider_tree_is_safe(
            b.tmux_target, "pi"
        ):
            return False
        if not tmux_respawn_pane(b.tmux_target, b.cwd):
            return False
        await asyncio.sleep(0.25)
        await self.ensure_running(b)
        return True

    async def hibernate(self, b: "Binding") -> bool:
        return await tmux_native_exit(
            b.tmux_target,
            "/quit",
            expected_commands=self.running_command_names,
            allowed_shells=self.shell_command_names,
        )

    async def reconcile_session_identity(self, b: "Binding") -> bool:
        """Read Pi's native `/session` screen after an interactive switch."""
        if b.pending_session_handoff_after is None:
            return False
        try:
            await tmux_send_text(
                b.tmux_target,
                "/session",
                expected_commands=self.running_command_names,
            )
            await asyncio.sleep(0.45)
            pane = strip_decorations(tmux_capture(b.tmux_target, 120))
        except Exception:
            log.exception("[%s] Pi session identity probe failed", b.name)
            return False
        session_match = re.search(r"^\s*ID:\s*([A-Za-z0-9-]+)\s*$", pane, re.M)
        file_match = re.search(r"^\s*File:\s*\n(?P<path>(?:\s+[^\n]+\n?)+?)^\s*ID:", pane, re.M)
        if session_match is None or file_match is None:
            return False
        session_id = session_match.group(1)
        path_text = "".join(line.strip() for line in file_match.group("path").splitlines())
        transcript = Path(path_text).expanduser()
        header = _session_header(transcript)
        if not header or str(header.get("id") or "") != session_id:
            return False
        try:
            actual_cwd = Path(str(header.get("cwd") or "")).expanduser().resolve()
        except OSError:
            return False
        if actual_cwd != b.cwd.expanduser().resolve():
            return False
        b.provider_session_id = session_id
        b.transcript_path = transcript
        b.last_session_id = session_id
        b.pending_session_handoff_after = None
        return True

    def interactive_session_handoff_commands(self) -> frozenset[str]:
        return frozenset({"/resume", "/fork", "/import"})

    def command_opts(self) -> dict[str, CmdOpts]:
        return {
            "/new": CmdOpts(
                init_delay=0.4,
                poll=0.3,
                max_iters=20,
                expect_new_session=True,
                defer_new_session_persistence=True,
                done_pattern=re.compile(r"✓\s*New session started", re.I),
                fallback_summary="✅ <b>Pi 新会话已启动</b>\n· 新会话将在首条回复落盘后绑定",
            ),
            "/clone": CmdOpts(
                init_delay=0.4,
                poll=0.3,
                max_iters=20,
                expect_new_session=True,
                expect_session_handoff=True,
                done_pattern=re.compile(r"Cloned to new session", re.I),
                fallback_summary="✅ <b>Pi 会话已克隆</b>",
                failure_summary="⚠️ <b>Pi clone 未确认完成</b>",
            ),
            "/compact": CmdOpts(
                init_delay=1.0,
                poll=0.5,
                max_iters=240,
                expect_compact_done=True,
                notice="⏳ Pi 压缩上下文中…",
                fallback_summary="✅ <b>Pi 上下文压缩已结束</b>",
                failure_summary=(
                    "⚠️ <b>Pi 未生成压缩记录</b>\n"
                    "· 当前会话可能太小、压缩被取消或 provider 返回错误；请用 /screen 查看 TUI"
                ),
            ),
            "/session": CmdOpts(init_delay=0.4, poll=0.3, max_iters=8, lines=120),
        }

    def command_aliases(self) -> dict[str, str]:
        return {}

    def compact_metadata_since(
        self, jsonl_path: Path | None, since_byte: int = 0
    ) -> dict | None:
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
            return {
                "preTokens": pre_tokens or None,
                "postTokens": _retained_tail_tokens(row.get("retainedTail")),
                "durationMs": None,
                "trigger": "manual",
            }
        return None

    def read_tasks(self, b: "Binding") -> list:
        transcript = self.find_active_jsonl(b)
        if transcript is None:
            return []
        latest: list[dict[str, Any]] | None = None
        try:
            raw_rows = transcript.read_text(
                encoding="utf-8", errors="replace"
            ).splitlines()
        except OSError:
            return []
        entries: list[dict[str, Any]] = []
        for line in raw_rows:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict) and row.get("type") != "session":
                entries.append(row)
        by_id = {
            str(row["id"]): row
            for row in entries
            if isinstance(row.get("id"), str) and row.get("id")
        }
        branch: list[dict[str, Any]] = []
        current = entries[-1] if entries else None
        seen: set[str] = set()
        while current is not None:
            branch.append(current)
            current_id = current.get("id")
            if isinstance(current_id, str):
                if current_id in seen:
                    break
                seen.add(current_id)
            parent_id = current.get("parentId")
            current = by_id.get(parent_id) if isinstance(parent_id, str) else None
        branch.reverse()
        for row in branch:
            if row.get("type") != "message":
                continue
            message = row.get("message") or {}
            if message.get("role") != "toolResult" or message.get("toolName") != "todo":
                continue
            details = message.get("details") or {}
            tasks = details.get("tasks")
            if isinstance(tasks, list) and isinstance(details.get("nextId"), int):
                latest = [dict(task) for task in tasks if isinstance(task, dict)]
        if latest is None:
            return []
        allowed = {"pending", "in_progress", "completed"}
        result = [task for task in latest if task.get("status") in allowed]
        result.sort(key=lambda task: int(task.get("id", 0) or 0))
        return result

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
            timestamp = _pi_timestamp(row.get("timestamp"))
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
        try:
            rows = jsonl_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return None
        total_input = total_output = cache_read = cache_write = count = 0
        last_ts = model = None
        latest_model_change = None
        for line in rows[-last_n * 5 :]:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("type") == "model_change":
                latest_model_change = row.get("modelId") or latest_model_change
                continue
            if row.get("type") != "message":
                continue
            message = row.get("message") or {}
            if message.get("role") != "assistant":
                continue
            usage = message.get("usage") or {}
            total_input += int(usage.get("input", 0) or 0)
            total_output += int(usage.get("output", 0) or 0)
            cache_read += int(usage.get("cacheRead", 0) or 0)
            cache_write += int(usage.get("cacheWrite", 0) or 0)
            count += 1
            last_ts = row.get("timestamp") or last_ts
            model = message.get("model") or model
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
            "model": model or latest_model_change or "pi",
        }
