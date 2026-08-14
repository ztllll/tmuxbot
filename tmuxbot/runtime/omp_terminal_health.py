"""Conservative, read-only OMP TUI suspected-stall auditing.

OMP deliberately retries provider failures and can compact/continue queued work after
one low-level run ends.  This module therefore never treats an error string or a
quiet transcript as a failure.  It emits one *suspected stall* notification only
when a live OMP TUI keeps rendering a real working spinner with no screen or
transcript progress for several ten-minute audits, while no known recovery work is
active.
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import json
import logging
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from tmuxbot.core.events import TerminalState
from tmuxbot.runtime.omp_errors import is_user_abort_error
from tmuxbot.runtime.omp_session_health import read_session_health
from tmuxbot.runtime.route_health import provider_tree_is_safe
from tmuxbot.tmux import tmux_capture, tmux_has_session
from tmuxbot.utils import strip_decorations

if TYPE_CHECKING:
    from tmuxbot.frontends.base import Frontend
    from tmuxbot.state import State

log = logging.getLogger("tmuxbot")

DEFAULT_OMP_TERMINAL_HEALTH_INTERVAL = 600.0
# One baseline followed by three unchanged ten-minute observations means a
# notification happens no sooner than thirty minutes after observable progress.
STALL_SAMPLES_BEFORE_NOTIFY = 3
_OMP_SPINNER_RE = re.compile(r"^[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏](?=\s)", re.M)
_STATUS_CLOCK_RE = re.compile(r"🕒\s*\d{1,2}:\d{2}")


def load_omp_terminal_health_registry(path: Path) -> dict[str, dict[str, Any]]:
    """Load durable notification dedupe state, treating malformed data as empty."""
    try:
        if path.is_symlink():
            return {}
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    routes = payload.get("routes")
    if not isinstance(routes, dict):
        return {}
    return {
        name: record
        for name, record in routes.items()
        if isinstance(name, str)
        and isinstance(record, dict)
        and isinstance(record.get("fingerprint"), str)
        and isinstance(record.get("session_id"), str)
        and isinstance(record.get("stalled_samples"), int)
        and isinstance(record.get("notified"), bool)
    }


def save_omp_terminal_health_registry(path: Path, registry: dict[str, dict[str, Any]]) -> None:
    """Atomically persist only the small, non-sensitive progress fingerprint state."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        temporary.write_text(
            json.dumps({"version": 1, "routes": registry}, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()
    os.chmod(path, 0o600)


def omp_terminal_health_enabled() -> bool:
    raw = os.getenv("TMUXBOT_OMP_TERMINAL_HEALTH_ENABLED", "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def omp_terminal_health_interval() -> float:
    raw = os.getenv("TMUXBOT_OMP_TERMINAL_HEALTH_INTERVAL", "")
    if not raw:
        return DEFAULT_OMP_TERMINAL_HEALTH_INTERVAL
    try:
        return max(60.0, float(raw))
    except ValueError:
        log.warning(
            "invalid TMUXBOT_OMP_TERMINAL_HEALTH_INTERVAL=%r, using %.1fs",
            raw,
            DEFAULT_OMP_TERMINAL_HEALTH_INTERVAL,
        )
        return DEFAULT_OMP_TERMINAL_HEALTH_INTERVAL


def provider_has_active_workload(target: str, executable: str) -> bool:
    """Whether OMP's process tree currently owns an active child workload.

    A OMP agent can legitimately hold a working spinner while its bash child runs.
    This observation is deliberately narrow: no matching child is not evidence of
    failure, while a matching live child is conclusive evidence that the audit must
    stay silent.
    """
    from tmuxbot.runtime.route_health import pane_processes

    return any(
        process.executable != executable
        and process.executable not in {"bash", "sh", "zsh", "fish"}
        and not process.stopped
        for process in pane_processes(target)
    )


def _progress_fingerprint(capture: str, transcript: Path) -> str | None:
    try:
        stat = transcript.stat()
    except OSError:
        return None
    # A braille frame proves the TUI renderer is alive, but it does not prove
    # the blocked provider request progresses.  Ignore that animation and the
    # footer clock so a silently stuck `Working...` can still be observed.
    visible = strip_decorations(capture).strip()
    visible = _OMP_SPINNER_RE.sub("⠿", visible)
    visible = _STATUS_CLOCK_RE.sub("🕒 --:--", visible)
    # mtime only indicates that OMP touched the file. Content size is the
    # durable progression signal: metadata-only timestamp changes must not
    # indefinitely hide a stuck request.
    stable = f"{stat.st_size}\0{visible}".encode("utf-8", "surrogateescape")
    return hashlib.sha256(stable).hexdigest()


def _reset(registry: dict[str, dict[str, Any]], route_name: str) -> None:
    registry.pop(route_name, None)


def _has_pending_session_handoff(state: object, binding: object) -> bool:
    return getattr(binding, "pending_session_handoff_after", None) is not None


def _is_compacting(state: object, binding: object) -> bool:
    return bool(getattr(state, "compaction_status", {}).get(binding.name))


def _identity_is_precise(backend: object, binding: object, transcript: Path) -> bool:
    session_id = getattr(binding, "provider_session_id", None)
    pinned_path = getattr(binding, "transcript_path", None)
    if not session_id or pinned_path is None or Path(pinned_path) != transcript:
        return False
    try:
        identity = backend.session_identity(binding, transcript)
    except Exception:
        return False
    return identity.session_id == session_id and Path(identity.transcript_path) == transcript


def _transcript_still_ends_in_error(transcript: Path, expected_message: str) -> bool:
    """Confirm no later user/successful assistant entry superseded the sidecar."""
    try:
        rows = transcript.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return False
    for line in reversed(rows[-256:]):
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        message = row.get("message") if isinstance(row, dict) else None
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        if role == "user":
            return False
        if role != "assistant":
            continue
        return (
            message.get("stopReason") == "error"
            and str(message.get("errorMessage") or "")[:500] == expected_message
        )
    return False


def _terminal_error_notice(binding: object, message: str) -> str:
    return (
        "❌ <b>OMP 已停止自动恢复，需要人工处理</b>\n"
        f"· route: <code>{html.escape(binding.name)}</code> · "
        f"target: <code>{html.escape(binding.tmux_target)}</code>\n"
        f"· 最后错误：{html.escape(message[:500])}\n"
        "· OMP 已结束 retry、compaction 和 follow-up；请用 <code>/screen</code> 查看 TUI。"
    )


def _notice(binding: object) -> str:
    return (
        "⚠️ <b>OMP 疑似失活，请人工查看</b>\n"
        f"· route: <code>{html.escape(binding.name)}</code> · "
        f"target: <code>{html.escape(binding.tmux_target)}</code>\n"
        "· OMP 持续显示工作中，但连续 3 次十分钟巡检未见屏幕或 JSONL 进展，"
        "且未检测到工具、重试、压缩或会话切换。\n"
        "· 这不是已确认错误；请用 <code>/screen</code> 查看该 TUI 后人工处理。"
    )


async def audit_omp_terminals_once(
    frontends: list["Frontend"], state: "State", registry: dict[str, dict[str, Any]]
) -> None:
    """Perform one fail-closed, non-destructive suspected-stall observation."""
    checked = silent = notified = 0
    for frontend in list(frontends):
        for binding in list(getattr(frontend, "bindings", ())):
            backend = frontend.backend_for(binding)
            if getattr(backend, "name", None) != "omp":
                continue
            if not tmux_has_session(binding.tmux_session):
                _reset(registry, binding.name)
                continue
            checked += 1
            session_health = read_session_health(binding.tmux_target, binding.cwd)
            if (
                session_health is not None
                and session_health.state == "terminal_error"
                and not is_user_abort_error(session_health.error_message)
                and session_health.session_id == binding.provider_session_id
                and Path(binding.transcript_path or "") == session_health.transcript_path
                and provider_tree_is_safe(binding.tmux_target, "omp")
                and _transcript_still_ends_in_error(
                    session_health.transcript_path,
                    session_health.error_message or "",
                )
            ):
                error_key = hashlib.sha256(
                    (
                        f"terminal-error\0{session_health.session_id}\0"
                        f"{session_health.response_id or session_health.error_message}\0"
                        f"{session_health.transcript_path}"
                    ).encode("utf-8", "surrogateescape")
                ).hexdigest()
                record = registry.get(binding.name)
                if record is None or record.get("terminal_error_key") != error_key:
                    try:
                        await frontend.send_html(
                            binding.chat_id,
                            binding.thread_id,
                            _terminal_error_notice(
                                binding,
                                session_health.error_message or "OMP provider request failed",
                            ),
                        )
                    except Exception:
                        log.exception("[%s] OMP terminal-error notification failed", binding.name)
                    else:
                        registry[binding.name] = {
                            "fingerprint": record.get("fingerprint", "") if record else "",
                            "session_id": session_health.session_id,
                            "stalled_samples": 0,
                            "notified": False,
                            "terminal_error_key": error_key,
                        }
                        notified += 1
                continue
            if (
                _has_pending_session_handoff(state, binding)
                or _is_compacting(state, binding)
                or not provider_tree_is_safe(binding.tmux_target, "omp")
                or provider_has_active_workload(binding.tmux_target, "omp")
            ):
                _reset(registry, binding.name)
                silent += 1
                continue
            try:
                capture = tmux_capture(binding.tmux_target, 80)
                status = backend.parse_terminal_status(capture)
                transcript = backend.find_active_jsonl(binding)
            except Exception:
                _reset(registry, binding.name)
                silent += 1
                continue
            if (
                status is None
                or status.state != TerminalState.WORKING
                or "retrying" in status.label.lower()
                or transcript is None
                or not _identity_is_precise(backend, binding, transcript)
            ):
                _reset(registry, binding.name)
                silent += 1
                continue
            fingerprint = _progress_fingerprint(capture, transcript)
            if fingerprint is None:
                _reset(registry, binding.name)
                silent += 1
                continue
            session_id = binding.provider_session_id
            record = registry.get(binding.name)
            if (
                record is None
                or record["fingerprint"] != fingerprint
                or record.get("session_id") != session_id
            ):
                registry[binding.name] = {
                    "fingerprint": fingerprint,
                    "session_id": session_id,
                    "stalled_samples": 0,
                    "notified": False,
                }
                continue
            record["stalled_samples"] += 1
            if record["notified"] or record["stalled_samples"] < STALL_SAMPLES_BEFORE_NOTIFY:
                continue
            try:
                await frontend.send_html(binding.chat_id, binding.thread_id, _notice(binding))
            except Exception:
                log.exception("[%s] OMP suspected-stall notification failed", binding.name)
                continue
            record["notified"] = True
            notified += 1
    log.debug(
        "OMP terminal-health tick · checked=%d silent=%d notified=%d", checked, silent, notified
    )


async def omp_terminal_health_audit_loop(
    frontends: list["Frontend"],
    state: "State",
    state_file: Path,
    *,
    interval: float | None = None,
) -> None:
    """Run OMP-only suspected-stall observation without touching tmux/provider state."""
    if not omp_terminal_health_enabled():
        log.info("OMP terminal-health audit disabled by TMUXBOT_OMP_TERMINAL_HEALTH_ENABLED")
        return
    every = interval if interval is not None else omp_terminal_health_interval()
    log.info("OMP terminal-health audit starting · interval=%.1fs", every)
    registry = load_omp_terminal_health_registry(state_file)
    while True:
        try:
            await audit_omp_terminals_once(frontends, state, registry)
            save_omp_terminal_health_registry(state_file, registry)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("OMP terminal-health audit tick failed")
        await asyncio.sleep(every)
