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
from tmuxbot.tmux import (
    tmux_capture,
    tmux_has_session,
    tmux_new_session,
    tmux_pane_command,
    tmux_safe_launch,
)
from tmuxbot.utils import strip_decorations

if TYPE_CHECKING:
    from tmuxbot.state import Binding

log = logging.getLogger("tmuxbot")

PI_SESSIONS_DIR = Path.home() / ".pi" / "agent" / "sessions"
_PI_WORKING_RE = re.compile(
    r"^\s*\S*\s*(?:Working|Compacting context|Auto-compacting|Summarizing branch|Retrying)\.\.\.",
    re.I | re.M,
)
_PI_MODEL_RE = re.compile(
    r"\b([A-Za-z0-9][A-Za-z0-9_.-]*)\s*[•·]\s*"
    r"(off|minimal|low|medium|high|xhigh|max|thinking off)\s*$",
    re.I | re.M,
)
_PI_CONTEXT_RE = re.compile(
    r"(?:(\d+(?:\.\d+)?)%|\?)\s*/\s*(\d+(?:\.\d+)?[kKmM]?)\s*(?:\(auto\))?"
)
_PI_CWD_RE = re.compile(r"^\s*(~(?:/\S*)?|/\S+?)(?:\s+\([^\n]+\))?\s*$", re.M)


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
        working = _PI_WORKING_RE.search(clean)
        model_match = _PI_MODEL_RE.search(clean)
        context_match = _PI_CONTEXT_RE.search(clean)
        cwd_match = _PI_CWD_RE.search(clean)
        context_limit = context_used = None
        if context_match:
            context_limit = _scaled_number(context_match.group(2))
            if context_match.group(1) is not None and context_limit:
                context_used = round(float(context_match.group(1)) / 100 * context_limit)
        effort = model = None
        if model_match:
            model = model_match.group(1)
            effort = model_match.group(2).lower().replace("thinking ", "")
        return TerminalStatus(
            state=TerminalState.WORKING if working else TerminalState.IDLE,
            label=working.group(0).strip() if working else "ready",
            model=model,
            effort=effort,
            cwd=cwd_match.group(1) if cwd_match else None,
            context_used=context_used,
            context_limit=context_limit,
        )

    def find_active_jsonl(self, b: "Binding") -> Path | None:
        directory = PI_SESSIONS_DIR / encode_pi_cwd(b.cwd)
        if not directory.is_dir():
            return None
        target_cwd = str(b.cwd.expanduser().resolve())
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
        if row.get("type") != "message":
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
        model = effort = None
        try:
            rows = transcript.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return ProviderRuntimeMetadata()
        for line in reversed(rows):
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if effort is None and row.get("type") == "thinking_level_change":
                effort = row.get("thinkingLevel")
            if model is None and row.get("type") == "model_change":
                model = row.get("modelId")
            if row.get("type") == "message":
                message = row.get("message") or {}
                if message.get("role") == "assistant":
                    model = model or message.get("model")
            if model is not None and effort is not None:
                break
        return ProviderRuntimeMetadata(
            model=str(model) if model else None,
            effort=str(effort) if effort else None,
        )

    def current_model(self, b: "Binding") -> str | None:
        return self.current_runtime_metadata(b).model

    def current_effort(self, b: "Binding") -> str | None:
        return self.current_runtime_metadata(b).effort

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
            return
        if not self.can_start_from_command(command):
            log.warning(
                "[%s] refusing to start Pi in pane with foreground command %r",
                b.name,
                command,
            )
            return
        launched = await tmux_safe_launch(
            b.tmux_target,
            _start_cmd(b.provider_session_id or b.last_session_id),
            allowed_shells=self.shell_command_names,
        )
        if not launched:
            log.warning("[%s] Pi launch aborted after foreground revalidation", b.name)
            return
        for _ in range(40):
            await asyncio.sleep(0.25)
            if self.is_running_command(tmux_pane_command(b.tmux_target)):
                try:
                    pane = tmux_capture(b.tmux_target, 12)
                except Exception:
                    pane = ""
                if "Working..." in pane or _PI_MODEL_RE.search(strip_decorations(pane)):
                    break

    def command_opts(self) -> dict[str, CmdOpts]:
        return {
            "/new": CmdOpts(init_delay=0.4, poll=0.3, max_iters=20, expect_new_session=True),
            "/compact": CmdOpts(
                init_delay=1.0,
                poll=0.5,
                max_iters=120,
                notice="⏳ Pi 压缩上下文中…",
                fallback_summary="✅ <b>Pi 上下文压缩已结束</b>",
            ),
            "/resume": CmdOpts(init_delay=0.5, poll=0.3, max_iters=8),
            "/session": CmdOpts(init_delay=0.4, poll=0.3, max_iters=8, lines=120),
        }

    def command_aliases(self) -> dict[str, str]:
        return {}

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
