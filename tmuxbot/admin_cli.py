"""Deterministic Admin operations for exact IM topic routes.

The Admin LLM supplies intent parameters.  This module owns preflight checks,
validated atomic route writes, supervised bridge restart, rollback, and runtime
verification so callers do not need to reproduce deployment mechanics.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from dotenv import dotenv_values

from tmuxbot.paths import default_admin_cwd
from tmuxbot.route_cli import (
    RouteStore,
    binding_from_mapping,
    binding_to_json,
    binding_to_mapping,
)
from tmuxbot.state import Binding
from tmuxbot.validation import ConfigValidationError, validate_bindings

_CONTRACT_START = "<!-- tmuxbot-admin-contract:start -->"
_CONTRACT_END = "<!-- tmuxbot-admin-contract:end -->"
_ADMIN_CONTEXT_VERSION = 1
_ADMIN_RUNBOOK = "ADMIN-RUNBOOK.md"
_ADMIN_MANIFEST = "tmuxbot-admin-context.json"
_TARGET_RE = re.compile(r"^(?P<session>[^:]+):(?P<window>\d+)\.(?P<pane>\d+)$")


class AdminOperationError(RuntimeError):
    """A safe Admin transaction could not be completed."""


@dataclass(frozen=True, slots=True)
class TmuxTarget:
    session: str
    window: int
    pane: int

    @property
    def value(self) -> str:
        return f"{self.session}:{self.window}.{self.pane}"


class AdminRuntime:
    """Small seam around local tmux and systemd user commands."""

    def run(self, argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(list(argv), capture_output=True, text=True)

    def target_status(self, target: TmuxTarget) -> dict[str, Any]:
        result = self.run(
            [
                "tmux",
                "display-message",
                "-p",
                "-t",
                target.value,
                "#{pane_current_path}\t#{pane_current_command}\t#{pane_dead}",
            ]
        )
        if result.returncode != 0 or not result.stdout.strip():
            return {"state": "stopped", "target": target.value}
        fields = result.stdout.rstrip("\n").split("\t")
        if len(fields) != 3:
            raise AdminOperationError(
                f"unable to inspect tmux target {target.value}: malformed tmux output"
            )
        cwd, command, dead = fields
        return {
            "state": "dead" if dead == "1" else "running",
            "target": target.value,
            "cwd": cwd,
            "command": command,
            "dead": dead == "1",
        }

    def list_targets(self) -> list[dict[str, Any]]:
        result = self.run(
            [
                "tmux",
                "list-panes",
                "-a",
                "-F",
                "#{session_name}:#{window_index}.#{pane_index}\t"
                "#{pane_current_path}\t#{pane_current_command}\t#{pane_dead}",
            ]
        )
        if result.returncode != 0:
            diagnostic = (result.stderr or result.stdout).strip().lower()
            if "no server running" in diagnostic:
                return []
            raise AdminOperationError(
                f"unable to list tmux panes: {(result.stderr or result.stdout).strip()}"
            )
        targets: list[dict[str, Any]] = []
        for line in result.stdout.splitlines():
            fields = line.split("\t")
            if len(fields) != 4:
                continue
            target, cwd, command, dead = fields
            targets.append(
                {
                    "target": target,
                    "cwd": cwd,
                    "command": command,
                    "dead": dead == "1",
                }
            )
        return targets

    def create_target(self, target: TmuxTarget, cwd: Path) -> None:
        if target.window != 0 or target.pane != 0:
            raise AdminOperationError(
                "--create-target currently creates only a new session target at window/pane 0.0"
            )
        session = self.run(["tmux", "has-session", "-t", target.session])
        if session.returncode == 0:
            raise AdminOperationError(
                f"tmux session {target.session!r} already exists but target {target.value} is missing"
            )
        created = self.run(
            ["tmux", "new-session", "-d", "-s", target.session, "-c", str(cwd)]
        )
        if created.returncode != 0:
            raise AdminOperationError(
                f"unable to create tmux target {target.value}: {created.stderr.strip()}"
            )

    def remove_created_target(self, target: TmuxTarget) -> None:
        removed = self.run(["tmux", "kill-session", "-t", target.session])
        if removed.returncode != 0:
            raise AdminOperationError(
                f"unable to remove newly created tmux session {target.session}: "
                f"{removed.stderr.strip()}"
            )

    def rename_session(self, old_name: str, new_name: str) -> None:
        renamed = self.run(["tmux", "rename-session", "-t", old_name, new_name])
        if renamed.returncode != 0:
            raise AdminOperationError(
                f"unable to rename tmux session {old_name!r} to {new_name!r}: "
                f"{renamed.stderr.strip()}"
            )

    def respawn_target(self, target: TmuxTarget, cwd: Path) -> None:
        respawned = self.run(
            ["tmux", "respawn-pane", "-k", "-t", target.value, "-c", str(cwd)]
        )
        if respawned.returncode != 0:
            raise AdminOperationError(
                f"unable to respawn tmux target {target.value}: {respawned.stderr.strip()}"
            )

    def restart_service(self, service: str) -> None:
        restarted = self.run(["systemctl", "--user", "restart", service])
        if restarted.returncode != 0:
            raise AdminOperationError(
                f"unable to restart {service}: {restarted.stderr.strip()}"
            )
        active = self.run(["systemctl", "--user", "is-active", service])
        if active.returncode != 0 or active.stdout.strip() != "active":
            raise AdminOperationError(f"service {service} did not become active")

    def service_status(self, service: str) -> dict[str, Any]:
        active = self.run(["systemctl", "--user", "is-active", service])
        return {
            "service": service,
            "active": active.returncode == 0 and active.stdout.strip() == "active",
            "state": active.stdout.strip() or active.stderr.strip() or "unknown",
        }


def parse_tmux_target(value: str) -> TmuxTarget:
    match = _TARGET_RE.fullmatch(value.strip())
    if match is None:
        raise AdminOperationError(
            "tmux target must use the exact form SESSION:WINDOW.PANE"
        )
    return TmuxTarget(
        session=match.group("session"),
        window=int(match.group("window")),
        pane=int(match.group("pane")),
    )


def _parse_identifier(value: str, *, channel: str) -> int | str:
    if channel == "telegram":
        try:
            return int(value)
        except ValueError as exc:
            raise AdminOperationError(
                "Telegram chat_id and thread_id must be integers"
            ) from exc
    value = value.strip()
    if not value:
        raise AdminOperationError("Feishu chat_id and thread_id must not be empty")
    return value


def _extract_text_values(value: Any) -> list[str]:
    values: list[str] = []
    if isinstance(value, dict):
        for key, nested in value.items():
            if key in {"title", "text", "content"} and isinstance(nested, str):
                stripped = re.sub(r"<[^>]+>", " ", nested)
                stripped = " ".join(stripped.split())
                if stripped:
                    values.append(stripped)
            else:
                values.extend(_extract_text_values(nested))
    elif isinstance(value, list):
        for nested in value:
            values.extend(_extract_text_values(nested))
    return values


def parse_telegram_topic_link(value: str) -> dict[str, Any]:
    parsed = urllib.parse.urlparse(value.strip())
    if parsed.scheme not in {"http", "https"} or parsed.netloc.lower() not in {
        "t.me",
        "www.t.me",
        "telegram.me",
        "www.telegram.me",
    }:
        raise AdminOperationError("Telegram topic link must use https://t.me/c/...")
    parts = [urllib.parse.unquote(part) for part in parsed.path.split("/") if part]
    if len(parts) not in {3, 4} or parts[0] != "c":
        raise AdminOperationError(
            "expected a private forum topic or message link in the form "
            "https://t.me/c/<internal-chat-id>/<thread-id>[/<message-id>]"
        )
    internal_chat_id, thread_id = parts[1:3]
    message_id = parts[3] if len(parts) == 4 else None
    identifiers = [internal_chat_id, thread_id]
    if message_id is not None:
        identifiers.append(message_id)
    if not all(part.isdigit() and int(part) > 0 for part in identifiers):
        raise AdminOperationError("Telegram link chat/thread/message identifiers must be positive integers")
    return {
        "channel": "telegram",
        "chat_id": int(f"-100{internal_chat_id}"),
        "thread_id": int(thread_id),
        "message_id": int(message_id) if message_id is not None else None,
        "message_link": value.strip(),
    }


def _telegram_token(env_file: Path, credential: str) -> str:
    values = dotenv_values(env_file)
    token = values.get(credential) or os.getenv(credential)
    if not token:
        raise AdminOperationError(f"missing {credential} in {env_file}")
    return str(token)


def _telegram_request_json(
    env_file: Path,
    credential: str,
    method: str,
    body: Mapping[str, Any],
) -> dict[str, Any]:
    token = _telegram_token(env_file, credential)
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=json.dumps(body, ensure_ascii=False).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.load(response)
    except Exception as exc:
        raise AdminOperationError(f"Telegram request failed: {exc}") from exc
    if payload.get("ok") is not True:
        raise AdminOperationError(
            "Telegram request failed: "
            f"code={payload.get('error_code')} description={payload.get('description')}"
        )
    return payload


def create_telegram_topic(
    env_file: Path,
    credential: str,
    chat_id: int,
    title: str,
) -> dict[str, Any]:
    title = " ".join(title.split())
    if not title:
        raise AdminOperationError("Telegram topic title must not be empty")
    payload = _telegram_request_json(
        env_file,
        credential,
        "createForumTopic",
        {"chat_id": chat_id, "name": title},
    )
    thread_id = (payload.get("result") or {}).get("message_thread_id")
    if not isinstance(thread_id, int) or thread_id <= 0:
        raise AdminOperationError("Telegram topic creation returned no thread ID")
    return {
        "title": title,
        "chat_id": chat_id,
        "thread_id": thread_id,
        "root_message_id": None,
    }


def delete_telegram_topic(
    env_file: Path,
    credential: str,
    chat_id: int,
    thread_id: int,
) -> None:
    _telegram_request_json(
        env_file,
        credential,
        "deleteForumTopic",
        {"chat_id": chat_id, "message_thread_id": thread_id},
    )


def _feishu_credentials(env_file: Path, credential: str) -> tuple[str, str]:
    values = dotenv_values(env_file)
    app_id = values.get(f"{credential}_APP_ID") or os.getenv(f"{credential}_APP_ID")
    app_secret = values.get(f"{credential}_APP_SECRET") or os.getenv(
        f"{credential}_APP_SECRET"
    )
    if not app_id or not app_secret:
        raise AdminOperationError(
            f"missing {credential}_APP_ID/{credential}_APP_SECRET in {env_file}"
        )
    return str(app_id), str(app_secret)


def _feishu_request_json(
    url: str,
    *,
    method: str | None = None,
    body: Mapping[str, Any] | None = None,
    token: str | None = None,
) -> dict[str, Any]:
    data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.load(response)
    except Exception as exc:
        raise AdminOperationError(f"Feishu request failed: {exc}") from exc
    if payload.get("code") not in {None, 0}:
        raise AdminOperationError(
            f"Feishu request failed: code={payload.get('code')} msg={payload.get('msg')}"
        )
    return payload


def _feishu_token(env_file: Path, credential: str) -> str:
    app_id, app_secret = _feishu_credentials(env_file, credential)
    auth = _feishu_request_json(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        body={"app_id": app_id, "app_secret": app_secret},
    )
    token = str(auth.get("tenant_access_token") or "")
    if not token:
        raise AdminOperationError("Feishu authentication returned no tenant token")
    return token


def _discover_feishu_topics_with_token(
    token: str, chat_id: str, *, limit: int = 50
) -> list[dict[str, Any]]:
    query = urllib.parse.urlencode(
        {
            "container_id_type": "chat",
            "container_id": chat_id,
            "sort_type": "ByCreateTimeDesc",
            "page_size": max(1, min(limit, 50)),
        }
    )
    payload = _feishu_request_json(
        f"https://open.feishu.cn/open-apis/im/v1/messages?{query}", token=token
    )
    topics: list[dict[str, Any]] = []
    seen: set[str] = set()
    for message in (payload.get("data") or {}).get("items") or []:
        thread_id = str(message.get("thread_id") or "")
        if not thread_id or thread_id in seen or message.get("root_id"):
            continue
        raw_content = (message.get("body") or {}).get("content") or ""
        try:
            content = json.loads(raw_content)
        except (TypeError, json.JSONDecodeError):
            content = raw_content
        texts = _extract_text_values(content)
        title = texts[0] if texts else ""
        sender = message.get("sender") or {}
        topics.append(
            {
                "title": title,
                "thread_id": thread_id,
                "root_message_id": message.get("message_id"),
                "create_time": message.get("create_time"),
                "sender_type": sender.get("sender_type"),
                "sender_id": sender.get("id"),
            }
        )
        seen.add(thread_id)
    return topics


def discover_feishu_topics(
    env_file: Path,
    credential: str,
    chat_id: str,
    *,
    limit: int = 50,
) -> list[dict[str, Any]]:
    return _discover_feishu_topics_with_token(
        _feishu_token(env_file, credential), chat_id, limit=limit
    )


def create_feishu_topic(
    env_file: Path,
    credential: str,
    chat_id: str,
    title: str,
    *,
    poll_attempts: int = 10,
    poll_interval: float = 0.5,
) -> dict[str, str]:
    title = " ".join(title.split())
    if not title:
        raise AdminOperationError("Feishu topic title must not be empty")
    token = _feishu_token(env_file, credential)
    query = urllib.parse.urlencode({"receive_id_type": "chat_id"})
    payload = _feishu_request_json(
        f"https://open.feishu.cn/open-apis/im/v1/messages?{query}",
        body={
            "receive_id": chat_id,
            "msg_type": "text",
            "content": json.dumps({"text": title}, ensure_ascii=False),
        },
        token=token,
    )
    root_message_id = str((payload.get("data") or {}).get("message_id") or "")
    if not root_message_id:
        raise AdminOperationError("Feishu topic creation returned no root message ID")
    for attempt in range(max(1, poll_attempts)):
        topics = _discover_feishu_topics_with_token(token, chat_id, limit=50)
        for topic in topics:
            if str(topic.get("root_message_id") or "") == root_message_id:
                thread_id = str(topic.get("thread_id") or "")
                if thread_id:
                    return {
                        "title": title,
                        "chat_id": chat_id,
                        "thread_id": thread_id,
                        "root_message_id": root_message_id,
                    }
        if attempt + 1 < max(1, poll_attempts) and poll_interval > 0:
            time.sleep(poll_interval)
    raise AdminOperationError(
        "Feishu created the root message but did not expose its thread_id in time; "
        f"root_message_id={root_message_id}"
    )


def delete_feishu_message(env_file: Path, credential: str, message_id: str) -> None:
    token = _feishu_token(env_file, credential)
    _feishu_request_json(
        f"https://open.feishu.cn/open-apis/im/v1/messages/{urllib.parse.quote(message_id)}",
        method="DELETE",
        token=token,
    )


def _parse_bool(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise argparse.ArgumentTypeError("expected true or false")


def _resolved_directory(value: str) -> Path:
    path = Path(value).expanduser().resolve()
    if not path.is_dir():
        raise AdminOperationError(f"cwd must be an existing directory: {path}")
    return path


def _require_feishu_thread_root(
    *, channel: str, thread_id: int | str | None, root_message_id: str | None
) -> None:
    if channel == "feishu" and thread_id is not None and not root_message_id:
        raise AdminOperationError(
            "Feishu topic routes require --thread-root-message-id from feishu-topics; "
            "it is the durable reply anchor for tmux-to-IM messages"
        )


def _route_item(args: argparse.Namespace, target: TmuxTarget, cwd: Path) -> dict[str, Any]:
    thread_id = _parse_identifier(args.thread_id, channel=args.channel)
    _require_feishu_thread_root(
        channel=args.channel,
        thread_id=thread_id,
        root_message_id=args.thread_root_message_id,
    )
    return {
        "name": args.name,
        "channel": args.channel,
        "bot_token_env": args.credential,
        "chat_id": _parse_identifier(args.chat_id, channel=args.channel),
        "thread_id": thread_id,
        "thread_root_message_id": args.thread_root_message_id,
        "tmux_session": target.session,
        "tmux_window": target.window,
        "tmux_pane": target.pane,
        "cwd": str(cwd),
        "backend": args.backend,
        "mention_required": args.mention_required,
    }


def _preflight_target(
    runtime: AdminRuntime,
    target: TmuxTarget,
    cwd: Path,
    *,
    create_target: bool,
) -> dict[str, Any]:
    status = runtime.target_status(target)
    if status["state"] == "running":
        actual_cwd = Path(str(status["cwd"])).expanduser().resolve()
        if actual_cwd != cwd:
            raise AdminOperationError(
                f"tmux target {target.value} cwd mismatch: {actual_cwd} != {cwd}"
            )
    elif status["state"] == "dead":
        raise AdminOperationError(f"tmux target {target.value} is dead")
    elif create_target:
        runtime.create_target(target, cwd)
        status = runtime.target_status(target)
        if status["state"] != "running":
            raise AdminOperationError(f"tmux target {target.value} was not created")
    return status


def _restore_file(path: Path, content: bytes, mode: int) -> None:
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=f".{path.name}.rollback.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
            temp_path = Path(stream.name)
        os.chmod(temp_path, mode)
        os.replace(temp_path, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()


def _apply_route_transaction(
    path: Path,
    service: str,
    runtime: AdminRuntime,
    store: RouteStore,
    write_route,
) -> tuple[Binding, dict[str, Any]]:
    if path.is_symlink():
        raise AdminOperationError(f"bindings file must not be a symbolic link: {path}")
    original = path.read_bytes()
    mode = path.stat().st_mode & 0o777
    try:
        binding = write_route()
        store.validate()
        runtime.restart_service(service)
        verification = verify_route(store, binding.name, service, runtime)
        if not verification["ok"]:
            raise AdminOperationError(
                "post-apply verification failed: " + "; ".join(verification["errors"])
            )
        return binding, verification
    except Exception:
        _restore_file(path, original, mode)
        try:
            runtime.restart_service(service)
        except Exception:
            pass
        raise


def _pi_session_identity(path: Path, cwd: Path) -> tuple[str, Path]:
    """Validate an exact Pi transcript supplied for a controlled route recovery."""
    transcript = path.expanduser().resolve()
    if not transcript.is_file():
        raise AdminOperationError(f"Pi session file does not exist: {transcript}")
    try:
        with transcript.open("r", encoding="utf-8", errors="replace") as stream:
            header = None
            for _ in range(32):
                line = stream.readline()
                if not line:
                    break
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(row, dict) and row.get("type") == "session":
                    header = row
                    break
    except OSError as exc:
        raise AdminOperationError(
            f"unable to read Pi session header from {transcript}: {exc}"
        ) from exc
    if header is None:
        raise AdminOperationError(f"Pi session file has no session header: {transcript}")
    session_id = str(header.get("id") or "").strip()
    if not session_id:
        raise AdminOperationError(f"Pi session file has no session id: {transcript}")
    try:
        actual_cwd = Path(str(header.get("cwd") or "")).expanduser().resolve()
    except OSError as exc:
        raise AdminOperationError(
            f"Pi session file has an invalid cwd: {transcript}"
        ) from exc
    if actual_cwd != cwd.expanduser().resolve():
        raise AdminOperationError(
            f"Pi session cwd mismatch: {actual_cwd} != {cwd.expanduser().resolve()}"
        )
    return session_id, transcript


def verify_route(
    store: RouteStore,
    name: str,
    service: str,
    runtime: AdminRuntime,
) -> dict[str, Any]:
    binding = store.inspect(name)
    target = parse_tmux_target(binding.tmux_target)
    target_status = runtime.target_status(target)
    errors: list[str] = []
    if target_status["state"] == "running":
        actual_cwd = Path(str(target_status["cwd"])).expanduser().resolve()
        expected_cwd = binding.cwd.expanduser().resolve()
        if actual_cwd != expected_cwd:
            errors.append(f"tmux cwd mismatch: {actual_cwd} != {expected_cwd}")
    elif target_status["state"] == "dead":
        errors.append(f"tmux target is dead: {binding.tmux_target}")
    service_status = runtime.service_status(service)
    if not service_status["active"]:
        errors.append(f"service is not active: {service}")
    return {
        "ok": not errors,
        "route": binding_to_json(binding),
        "tmux": target_status,
        "service": service_status,
        "errors": errors,
    }


def render_contract(*, bindings_file: Path, service: str) -> str:
    env_file = bindings_file.expanduser().resolve().parent / ".env"
    return f"""# tmuxbot Admin Operations Contract

## Identity and runtime

You are the dedicated tmuxbot Admin DM management agent, not an ordinary project
assistant. The authenticated operator's private IM messages are injected into a
real interactive Pi, Claude Code, or Codex TUI in this exact tmux pane. tmuxbot
reads that provider's local transcript and returns your output to the same exact
DM endpoint; it does not call a separate headless model API.

This Admin cwd is a context anchor, not a permission sandbox. You run with the
Unix permissions of the account that owns tmuxbot. Project topics use separate
exact routes, panes, cwd values, and provider sessions.

## Responsibilities

Primary responsibilities:
- inspect tmuxbot routes, panes, providers, logs, and user services;
- provision, bind, move, rename, and verify project topic routes;
- diagnose delivery, transcript identity, provider-session, and bridge failures;
- maintain tmuxbot-owned operational state safely;
- explain the current tmuxbot deployment and architecture to the operator.

Ordinary project implementation belongs in that project's own topic/pane. Work
outside tmuxbot administration only when the operator explicitly requests it.
For non-routine administration or troubleshooting, read `{_ADMIN_RUNBOOK}` first.

## Deterministic route operations

You manage exact IM endpoint -> tmux pane routes. Use tmuxbot's deterministic
Admin commands; do not reconstruct YAML/systemd/tmux transactions by hand.

Canonical objects:
- endpoint = channel + credential + chat_id + thread_id
- target = tmux SESSION:WINDOW.PANE + cwd
- adapter = claude_code | codex | pi
- route = one exact endpoint mapped to one exact target and adapter

Hard rules:
1. Never guess a topic/thread ID. For a Feishu topic-originated Admin request,
   the bridge supplies the verified chat_id/thread_id/root_message_id in the
   injected request; use those exact values. Otherwise ask for the ID or a
   message link. Never create a new topic unless the user explicitly asks.
2. A group root and every topic/thread are different endpoints. Unbound endpoints
   stay silent and must not touch tmux.
3. Project creation and existing-topic binding both use `provision-project`.
   Run its plan first, repeat with --apply after all values match, then perform
   live IM acceptance. Do not make the LLM assemble discovery/bind steps itself.
4. Do not reuse a pane with a different cwd. Do not bind two routes to one pane.
5. Do not run `tmux kill-server`. Moving a route preserves its provider identity.
6. Project topics normally use `--mention-required false`.
7. Never create a tmux target before a valid provisioning plan. Do not run
   `tmux new-session` directly for a route; `provision-project --apply` creates a
   missing NAME:0.0 target transactionally and reuses only an exact-cwd target.
8. Config changes are complete only after validated atomic write, supervised
   bridge restart, and verification. The Admin command owns those steps.
9. `provision-project` is the normal high-level interface for Telegram and
   Feishu, whether the topic already exists or must be created. `create-topic`,
   `bind-topic`, and discovery commands are low-level recovery/diagnostic tools.
10. If Pi was switched outside the channel command flow and replies stop, use
   `adopt-pi-session` with the exact new JSONL path: run its plan, then `--apply`.
   It verifies the session header and exact cwd; never guess or select by mtime.
11. Telegram topic routes need only chat_id + thread_id. Accept either
    https://t.me/c/CHAT/THREAD or a full message link; never demand a message ID
    or thread_root_message_id for Telegram.
12. IM file delivery is supported by the bridge. When the operator asks to send
    an existing local file, do not claim this interface cannot send files. Reply
    with the file as a standalone Markdown link using its absolute path, e.g.
    `[download](</absolute/path/report.pdf>)`. The bridge removes that path from
    text and uploads the file to the same exact endpoint. Only reference a real,
    readable file under the route cwd or `/tmp/tmuxbot-attachments`; otherwise
    state the missing-file error plainly.
13. A change is not complete until the deterministic command reports successful
    verification and the operator performs live IM acceptance where required.
14. Never treat this cwd as the tmuxbot source checkout. Use the deployment paths
    below and inspect the runbook before editing code or operational state.

Deployment:
- bindings: {bindings_file}
- supervised bridge: {service}

Feishu topic-originated workflow:
```text
Boss @mentions the Admin bot inside the destination topic with a request that
contains “创建项目” or /create-project. The bridge injects that topic's verified
chat_id, thread_id and thread_root_message_id below. Use those values in
provision-project; never rediscover or guess a parent group.
```

Required normal workflow:
```bash
# Existing Telegram topic URL (message ID optional):
tmuxbot admin --file {bindings_file} --service {service} provision-project \
  --name ROUTE --channel telegram --credential TG_CODEX_BOT_TOKEN \
  --topic-link https://t.me/c/INTERNAL_CHAT_ID/THREAD_ID \
  --cwd /absolute/project --backend pi
# New Telegram or Feishu topic: replace --topic-link with exact --chat-id and
# --topic-title. tmux target defaults to NAME:0.0. Review the plan, then repeat
# the same command with --apply. A Feishu topic-originated Admin request already
# carries verified --chat-id/--thread-id/--thread-root-message-id; do not guess.
```

Low-level recovery and diagnostics:
```bash
tmuxbot admin --file {bindings_file} --service {service} inventory --json
# For Telegram private forums, a topic URL is enough; message ID is optional:
tmuxbot admin --file {bindings_file} --service {service} telegram-topic \\
  --message-link https://t.me/c/INTERNAL_CHAT_ID/THREAD_ID --json
# For Feishu, discover exact existing topics instead of guessing:
tmuxbot admin --file {bindings_file} --service {service} feishu-topics \\
  --env-file {env_file} --credential FEISHU_CODEX --chat-id oc_xxx --json
# Explicit new Telegram/Feishu topic: one plan covers topic + target + route:
tmuxbot admin --file {bindings_file} --service {service} create-topic \\
  --env-file {env_file} --name ROUTE --channel feishu \\
  --credential FEISHU_CODEX --chat-id oc_xxx --topic-title "Project topic" \\
  --tmux-target project:0.0 --cwd /absolute/project --backend pi \\
  --mention-required false --create-target

tmuxbot admin --file {bindings_file} --service {service} bind-topic \\
  --name ROUTE --channel feishu --credential FEISHU_CODEX \\
  --chat-id oc_xxx --thread-id omt_xxx --thread-root-message-id om_xxx \\
  --tmux-target project:0.0 \\
  --cwd /absolute/project --backend pi --mention-required false
# Inspect the plan, then repeat with --apply.

tmuxbot admin --file {bindings_file} --service {service} move-topic ROUTE \\
  --channel feishu --chat-id oc_xxx --thread-id omt_xxx \\
  --thread-root-message-id om_xxx
# Inspect the plan, then repeat with --apply.

# Recovery after a direct Pi TUI session switch: inspect the plan, then add --apply.
tmuxbot admin --file {bindings_file} --service {service} adopt-pi-session ROUTE \\
  --session-file /absolute/pi-session.jsonl
tmuxbot admin --file {bindings_file} --service {service} verify ROUTE --json
```

If a required value is missing or ambiguous, stop and ask the operator. A safe
refusal is correct; guessing a route is not.
"""


def render_admin_runbook(*, bindings_file: Path, service: str, cwd: Path) -> str:
    bindings = bindings_file.expanduser().resolve()
    env_file = bindings.parent / ".env"
    return f"""# tmuxbot Admin DM Runbook

This file is generated by `tmuxbot admin install-contract`. Re-run that command
after changing the deployment, bindings path, service name, or Admin workspace.

## Current deployment

- Admin workspace: `{cwd}`
- bindings: `{bindings}`
- environment file: `{env_file}`
- supervised bridge: `{service}`
- Admin target: configured by `TMUXBOT_ADMIN_TMUX` (normally `tmuxbot-admin:0.0`)
- source checkout: not implied by this workspace; locate it only when code work is requested

## How this conversation runs

1. The authenticated Boss sends a private Telegram or Feishu message.
2. tmuxbot resolves the exact Admin endpoint and injects text into this real tmux pane.
3. Pi, Claude Code, or Codex runs interactively with the Unix account's permissions.
4. tmuxbot tails the exact provider transcript and returns output to the same DM.
5. Project topics are separate endpoint -> pane routes; an unbound endpoint is silent.

The workspace is intentionally outside the home-directory root and project trees
so its Admin instructions do not leak into ordinary project sessions. It is a
context anchor, not a sandbox or source checkout.

## Operational scope

Use this session for route lifecycle, tmux/provider inspection, bridge and user
systemd diagnosis, transcript identity recovery, deployment explanation, and
safe maintenance of tmuxbot-owned state. Send ordinary project development to
that project's own topic/pane unless the operator explicitly asks this Admin
session to perform it.

## Normal workflow

```text
collect exact endpoint intent + cwd + adapter
-> tmuxbot admin provision-project ...          # plan only
-> review endpoint, target action, cwd, adapter
-> repeat the exact command with --apply
-> command-owned atomic write, restart, verify, rollback on failure
-> operator sends a real message in the bound topic for live acceptance
```

Never guess a topic/thread ID, create a topic without explicit permission, create
a route pane before a valid plan, reuse a different-cwd pane, bind two routes to
one pane, or run `tmux kill-server`.

## Diagnostics and recovery

```bash
tmuxbot admin --file {bindings} --service {service} inventory --json
tmuxbot admin --file {bindings} --service {service} verify ROUTE --json
tmuxbot admin --file {bindings} --service {service} telegram-topic \\
  --message-link https://t.me/c/CHAT/THREAD --json
tmuxbot admin --file {bindings} --service {service} feishu-topics \\
  --env-file {env_file} --credential FEISHU_CODEX --chat-id oc_xxx --json
```

Use `adopt-pi-session` only after an operator switches the live Pi TUI outside
tmuxbot's channel command flow. Require the exact JSONL path; never select one by
mtime. Lower-level `create-topic`, `bind-topic`, and discovery commands are for
recovery or diagnosis when `provision-project` does not cover the operation.

## File delivery

To send an existing readable local file back through the current DM, return a
standalone absolute Markdown link such as:

```markdown
[download](</absolute/path/report.pdf>)
```

The bridge uploads it only when the path is under the route cwd,
`/tmp/tmuxbot-attachments`, or another configured allowed root.

## Acceptance checklist

- complete candidate route validation passed;
- exact endpoint, credential, cwd, target, and adapter match the operator's intent;
- supervised bridge is active after the transaction;
- route verification reports no errors;
- real DM/topic input reaches only the intended pane;
- assistant output returns to the same exact endpoint;
- group roots and unbound topics remain silent.
"""


def _managed_contract_block(*, bindings_file: Path, service: str) -> str:
    body = render_contract(bindings_file=bindings_file, service=service).strip()
    return f"{_CONTRACT_START}\n{body}\n{_CONTRACT_END}\n"


def _install_managed_block(path: Path, block: str) -> None:
    original = path.read_text(encoding="utf-8") if path.exists() else ""
    start = original.find(_CONTRACT_START)
    end = original.find(_CONTRACT_END)
    if start >= 0 and end >= start:
        end += len(_CONTRACT_END)
        while end < len(original) and original[end] in "\r\n":
            end += 1
        rendered = original[:start] + block + original[end:]
    else:
        separator = "" if not original or original.endswith("\n") else "\n"
        rendered = original + separator + block
    temp_path: Path | None = None
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            stream.write(rendered)
            stream.flush()
            os.fsync(stream.fileno())
            temp_path = Path(stream.name)
        os.chmod(temp_path, 0o600)
        os.replace(temp_path, path)
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()


def _write_private_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
            temp_path = Path(stream.name)
        os.chmod(temp_path, 0o600)
        os.replace(temp_path, path)
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()


def _admin_context_payload(
    *, cwd: Path, bindings_file: Path, service: str, contract: str, runbook: str
) -> dict[str, Any]:
    return {
        "schema_version": _ADMIN_CONTEXT_VERSION,
        "admin_cwd": str(cwd),
        "bindings_file": str(bindings_file.expanduser().resolve()),
        "env_file": str(bindings_file.expanduser().resolve().parent / ".env"),
        "service": service,
        "files": {
            "AGENTS.md": hashlib.sha256(contract.encode()).hexdigest(),
            "CLAUDE.md": hashlib.sha256(contract.encode()).hexdigest(),
            _ADMIN_RUNBOOK: hashlib.sha256(runbook.encode()).hexdigest(),
        },
    }


def install_admin_context(*, cwd: Path, bindings_file: Path, service: str) -> list[Path]:
    resolved_cwd = cwd.expanduser().resolve()
    try:
        resolved_cwd.mkdir(parents=True, exist_ok=True, mode=0o700)
        if resolved_cwd.is_symlink() or not resolved_cwd.is_dir():
            raise OSError("path is not a real directory")
        os.chmod(resolved_cwd, 0o700)
    except OSError as exc:
        raise AdminOperationError(
            f"unable to prepare private Admin context cwd {resolved_cwd}: {exc}"
        ) from exc
    block = _managed_contract_block(bindings_file=bindings_file, service=service)
    runbook = render_admin_runbook(
        bindings_file=bindings_file, service=service, cwd=resolved_cwd
    )
    installed = [resolved_cwd / "AGENTS.md", resolved_cwd / "CLAUDE.md"]
    for path in installed:
        _install_managed_block(path, block)
    runbook_path = resolved_cwd / _ADMIN_RUNBOOK
    _write_private_text(runbook_path, runbook)
    manifest = _admin_context_payload(
        cwd=resolved_cwd,
        bindings_file=bindings_file,
        service=service,
        contract=block,
        runbook=runbook,
    )
    manifest_path = resolved_cwd / _ADMIN_MANIFEST
    _write_private_text(
        manifest_path,
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
    )
    verification = verify_admin_context(
        cwd=resolved_cwd, bindings_file=bindings_file, service=service
    )
    if not verification["ok"]:
        raise AdminOperationError(
            "Admin context verification failed: " + "; ".join(verification["errors"])
        )
    return [*installed, runbook_path, manifest_path]


def verify_admin_context(*, cwd: Path, bindings_file: Path, service: str) -> dict[str, Any]:
    resolved_cwd = cwd.expanduser().resolve()
    expected_block = _managed_contract_block(bindings_file=bindings_file, service=service)
    expected_runbook = render_admin_runbook(
        bindings_file=bindings_file, service=service, cwd=resolved_cwd
    )
    expected = _admin_context_payload(
        cwd=resolved_cwd,
        bindings_file=bindings_file,
        service=service,
        contract=expected_block,
        runbook=expected_runbook,
    )
    errors: list[str] = []
    manifest_path = resolved_cwd / _ADMIN_MANIFEST
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        manifest = None
        errors.append(f"missing or invalid {_ADMIN_MANIFEST}: {exc}")
    for name, expected_hash in expected["files"].items():
        path = resolved_cwd / name
        try:
            content = path.read_text(encoding="utf-8")
        except OSError as exc:
            errors.append(f"missing {name}: {exc}")
            continue
        if name in {"AGENTS.md", "CLAUDE.md"}:
            start = content.find(_CONTRACT_START)
            end = content.find(_CONTRACT_END)
            if start < 0 or end < start:
                errors.append(f"managed contract block missing from {name}")
                continue
            end += len(_CONTRACT_END)
            while end < len(content) and content[end] in "\r\n":
                end += 1
            content = content[start:end]
        actual_hash = hashlib.sha256(content.encode()).hexdigest()
        if actual_hash != expected_hash:
            errors.append(f"stale or modified {name}")
    if manifest != expected:
        errors.append(f"stale or mismatched {_ADMIN_MANIFEST}")
    return {
        "ok": not errors,
        "cwd": str(resolved_cwd),
        "schema_version": _ADMIN_CONTEXT_VERSION,
        "manifest": str(manifest_path),
        "errors": errors,
    }


def build_admin_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tmuxbot admin")
    parser.add_argument("--file", type=Path, required=True, dest="bindings_file")
    parser.add_argument("--service", default="tmuxbot.service")
    subparsers = parser.add_subparsers(dest="admin_command", required=True)

    subparsers.add_parser("contract", help="print the Admin Operations Contract")
    inventory = subparsers.add_parser(
        "inventory", help="list current routes and real tmux panes"
    )
    inventory.add_argument("--json", action="store_true", dest="as_json")
    telegram_topic = subparsers.add_parser(
        "telegram-topic", help="parse an exact private Telegram forum message link"
    )
    telegram_topic.add_argument("--message-link", required=True)
    telegram_topic.add_argument("--json", action="store_true", dest="as_json")
    topics = subparsers.add_parser(
        "feishu-topics", help="discover recent Feishu topic IDs without changing routes"
    )
    topics.add_argument("--env-file", type=Path, required=True)
    topics.add_argument("--credential", required=True)
    topics.add_argument("--chat-id", required=True)
    topics.add_argument("--limit", type=int, default=50)
    topics.add_argument("--json", action="store_true", dest="as_json")
    create_topic = subparsers.add_parser(
        "create-topic",
        aliases=("create-feishu-topic",),
        help="plan or apply one Telegram/Feishu topic + target + route transaction",
    )
    create_topic.add_argument("--env-file", type=Path, required=True)
    create_topic.add_argument("--name", required=True)
    create_topic.add_argument(
        "--channel", choices=("telegram", "feishu"), default="feishu"
    )
    create_topic.add_argument("--credential", required=True)
    create_topic.add_argument("--chat-id", required=True)
    create_topic.add_argument("--topic-title", required=True)
    create_topic.add_argument("--tmux-target", required=True)
    create_topic.add_argument("--cwd", required=True)
    create_topic.add_argument(
        "--backend", choices=("claude_code", "codex", "pi"), required=True
    )
    create_topic.add_argument("--mention-required", type=_parse_bool, default=False)
    create_topic.add_argument("--create-target", action="store_true")
    create_topic.add_argument("--apply", action="store_true")
    provision = subparsers.add_parser(
        "provision-project",
        help="plan or apply one fixed endpoint + tmux + route provisioning workflow",
    )
    provision.add_argument("--env-file", type=Path)
    provision.add_argument("--name", required=True)
    provision.add_argument("--channel", choices=("telegram", "feishu"), required=True)
    provision.add_argument("--credential", required=True)
    provision.add_argument("--chat-id")
    provision.add_argument("--thread-id")
    provision.add_argument("--thread-root-message-id")
    provision.add_argument("--topic-link")
    provision.add_argument("--topic-title")
    provision.add_argument("--tmux-target")
    provision.add_argument("--cwd", required=True)
    provision.add_argument(
        "--backend", choices=("claude_code", "codex", "pi"), required=True
    )
    provision.add_argument("--mention-required", type=_parse_bool, default=False)
    provision.add_argument("--apply", action="store_true")
    install = subparsers.add_parser(
        "install-contract",
        help="install/update the dedicated Admin context, runbook, and manifest",
    )
    install.add_argument("--cwd", type=Path, default=default_admin_cwd(os.environ))
    verify_context = subparsers.add_parser(
        "verify-context", help="verify the dedicated Admin context version and file hashes"
    )
    verify_context.add_argument("--cwd", type=Path, default=default_admin_cwd(os.environ))
    verify_context.add_argument("--json", action="store_true", dest="as_json")

    bind = subparsers.add_parser("bind-topic", help="plan or apply a new exact topic route")
    bind.add_argument("--name", required=True)
    bind.add_argument("--channel", choices=("telegram", "feishu"), required=True)
    bind.add_argument("--credential", required=True)
    bind.add_argument("--chat-id", required=True)
    bind.add_argument("--thread-id", required=True)
    bind.add_argument("--thread-root-message-id")
    bind.add_argument("--tmux-target", required=True)
    bind.add_argument("--cwd", required=True)
    bind.add_argument("--backend", choices=("claude_code", "codex", "pi"), required=True)
    bind.add_argument("--mention-required", type=_parse_bool, default=False)
    bind.add_argument("--create-target", action="store_true")
    bind.add_argument("--apply", action="store_true")

    move = subparsers.add_parser(
        "move-topic", help="plan or apply an endpoint move while preserving route identity"
    )
    move.add_argument("name")
    move.add_argument("--channel", choices=("telegram", "feishu"), required=True)
    move.add_argument("--chat-id", required=True)
    move.add_argument("--thread-id", required=True)
    move.add_argument("--thread-root-message-id")
    move.add_argument("--apply", action="store_true")

    rename_project = subparsers.add_parser(
        "rename-project",
        help="plan or atomically rename one route, single-pane tmux session, and cwd",
    )
    rename_project.add_argument("name")
    rename_project.add_argument("--new-name", required=True)
    rename_project.add_argument("--new-cwd", type=Path, required=True)
    rename_project.add_argument("--apply", action="store_true")

    adopt_pi = subparsers.add_parser(
        "adopt-pi-session",
        help="plan or adopt one exact Pi session file after an out-of-band Pi session switch",
    )
    adopt_pi.add_argument("name")
    adopt_pi.add_argument("--session-file", type=Path, required=True)
    adopt_pi.add_argument("--apply", action="store_true")
    verify = subparsers.add_parser("verify", help="verify route, tmux target, and bridge")
    verify.add_argument("name")
    verify.add_argument("--json", action="store_true", dest="as_json")
    return parser


def run_admin_command(
    argv: Sequence[str], *, runtime: AdminRuntime | None = None
) -> int:
    args = build_admin_parser().parse_args(list(argv))
    runtime = runtime or AdminRuntime()
    store = RouteStore(args.bindings_file)
    try:
        if args.admin_command == "contract":
            print(render_contract(bindings_file=args.bindings_file, service=args.service))
            return 0
        if args.admin_command == "inventory":
            routes = [binding_to_json(binding) for binding in store.list()]
            payload = {"routes": routes, "tmux_targets": runtime.list_targets()}
            print(
                json.dumps(
                    payload,
                    ensure_ascii=False,
                    indent=None if args.as_json else 2,
                )
            )
            return 0
        if args.admin_command == "telegram-topic":
            topic = parse_telegram_topic_link(args.message_link)
            print(
                json.dumps(
                    topic,
                    ensure_ascii=False,
                    indent=None if args.as_json else 2,
                )
            )
            return 0
        if args.admin_command == "feishu-topics":
            topics = discover_feishu_topics(
                args.env_file.expanduser(),
                args.credential,
                args.chat_id,
                limit=args.limit,
            )
            print(
                json.dumps(
                    topics,
                    ensure_ascii=False,
                    indent=None if args.as_json else 2,
                )
            )
            return 0
        if args.admin_command in {"create-topic", "create-feishu-topic"}:
            channel = args.channel
            target = parse_tmux_target(args.tmux_target)
            cwd = _resolved_directory(args.cwd)
            chat_id = _parse_identifier(args.chat_id, channel=channel)
            title = " ".join(args.topic_title.split())
            if not title:
                raise AdminOperationError(f"{channel.title()} topic title must not be empty")
            if any(binding.name == args.name for binding in store.list()):
                raise AdminOperationError(f"route name already exists: {args.name!r}")
            if any(binding.tmux_target == target.value for binding in store.list()):
                raise AdminOperationError(f"duplicate tmux target: {target.value}")
            target_status = _preflight_target(
                runtime, target, cwd, create_target=False
            )
            plan = {
                "operation": "create-topic",
                "apply": args.apply,
                "endpoint": {
                    "channel": channel,
                    "credential": args.credential,
                    "chat_id": chat_id,
                    "title": title,
                    "thread_id": f"<returned by {channel.title()} on apply>",
                    "thread_root_message_id": (
                        "<returned by Feishu on apply>" if channel == "feishu" else None
                    ),
                },
                "route": {
                    "name": args.name,
                    "tmux_target": target.value,
                    "cwd": str(cwd),
                    "backend": args.backend,
                    "mention_required": args.mention_required,
                },
                "tmux": target_status,
                "create_target": args.create_target,
                "service": args.service,
                "rollback": [
                    "restore bindings",
                    "restart previous bridge",
                    "remove transaction-created tmux session",
                    f"delete transaction-created {channel.title()} topic",
                ],
            }
            if not args.apply:
                print(json.dumps(plan, ensure_ascii=False, indent=2))
                print("plan only: repeat with --apply after verifying every value")
                return 0
            if target_status["state"] == "stopped" and not args.create_target:
                raise AdminOperationError(
                    f"tmux target {target.value} does not exist; pass --create-target explicitly"
                )
            topic: dict[str, Any] | None = None
            created_target = False
            try:
                if channel == "telegram":
                    topic = create_telegram_topic(
                        args.env_file.expanduser(), args.credential, int(chat_id), title
                    )
                else:
                    topic = create_feishu_topic(
                        args.env_file.expanduser(), args.credential, str(chat_id), title
                    )
                item = {
                    "name": args.name,
                    "channel": channel,
                    "bot_token_env": args.credential,
                    "chat_id": chat_id,
                    "thread_id": topic["thread_id"],
                    "thread_root_message_id": topic["root_message_id"],
                    "tmux_session": target.session,
                    "tmux_window": target.window,
                    "tmux_pane": target.pane,
                    "cwd": str(cwd),
                    "backend": args.backend,
                    "mention_required": args.mention_required,
                }
                validate_bindings([*store.list(), binding_from_mapping(item)])
                if args.create_target and target_status["state"] == "stopped":
                    _preflight_target(runtime, target, cwd, create_target=True)
                    created_target = True
                _bound, verification = _apply_route_transaction(
                    args.bindings_file,
                    args.service,
                    runtime,
                    store,
                    lambda: store.bind(item),
                )
            except Exception:
                if created_target:
                    try:
                        runtime.remove_created_target(target)
                    except Exception:
                        pass
                if topic is not None:
                    try:
                        if channel == "telegram":
                            delete_telegram_topic(
                                args.env_file.expanduser(),
                                args.credential,
                                int(chat_id),
                                int(topic["thread_id"]),
                            )
                        else:
                            delete_feishu_message(
                                args.env_file.expanduser(),
                                args.credential,
                                str(topic["root_message_id"]),
                            )
                    except Exception:
                        pass
                raise
            verification["created_topic"] = topic
            print(json.dumps(verification, ensure_ascii=False))
            return 0
        if args.admin_command == "provision-project":
            target = parse_tmux_target(args.tmux_target or f"{args.name}:0.0")
            cwd = _resolved_directory(args.cwd)
            env_file = (
                args.env_file.expanduser()
                if args.env_file is not None
                else args.bindings_file.expanduser().resolve().parent / ".env"
            )
            intents = sum(
                (
                    bool(args.topic_title),
                    bool(args.topic_link),
                    bool(args.chat_id and args.thread_id),
                )
            )
            if intents != 1:
                raise AdminOperationError(
                    "provision-project requires exactly one topic intent: "
                    "--topic-title with --chat-id, --topic-link, or --chat-id + --thread-id"
                )
            topic_mode = "create" if args.topic_title else "existing"
            topic_title = " ".join((args.topic_title or "").split()) or None
            if topic_mode == "create":
                if not args.chat_id:
                    raise AdminOperationError("new topics require exact --chat-id")
                chat_id = _parse_identifier(args.chat_id, channel=args.channel)
                thread_id: int | str | None = None
                root_message_id = None
            elif args.topic_link:
                if args.channel != "telegram":
                    raise AdminOperationError(
                        "--topic-link currently accepts Telegram private forum URLs only; "
                        "use Feishu --chat-id + --thread-id + --thread-root-message-id"
                    )
                parsed = parse_telegram_topic_link(args.topic_link)
                chat_id = parsed["chat_id"]
                thread_id = parsed["thread_id"]
                root_message_id = None
            else:
                chat_id = _parse_identifier(args.chat_id, channel=args.channel)
                thread_id = _parse_identifier(args.thread_id, channel=args.channel)
                root_message_id = args.thread_root_message_id
                _require_feishu_thread_root(
                    channel=args.channel,
                    thread_id=thread_id,
                    root_message_id=root_message_id,
                )
            if any(binding.name == args.name for binding in store.list()):
                raise AdminOperationError(f"route name already exists: {args.name!r}")
            if any(binding.tmux_target == target.value for binding in store.list()):
                raise AdminOperationError(f"duplicate tmux target: {target.value}")
            target_status = _preflight_target(
                runtime, target, cwd, create_target=False
            )
            item_base = {
                "name": args.name,
                "channel": args.channel,
                "bot_token_env": args.credential,
                "chat_id": chat_id,
                "thread_id": thread_id,
                "thread_root_message_id": root_message_id,
                "tmux_session": target.session,
                "tmux_window": target.window,
                "tmux_pane": target.pane,
                "cwd": str(cwd),
                "backend": args.backend,
                "mention_required": args.mention_required,
            }
            if topic_mode == "existing":
                validate_bindings([*store.list(), binding_from_mapping(item_base)])
            plan = {
                "operation": "provision-project",
                "apply": args.apply,
                "endpoint": {
                    "mode": topic_mode,
                    "channel": args.channel,
                    "credential": args.credential,
                    "chat_id": chat_id,
                    "thread_id": thread_id,
                    "thread_root_message_id": root_message_id,
                    "topic_title": topic_title,
                },
                "route": {
                    "name": args.name,
                    "tmux_target": target.value,
                    "cwd": str(cwd),
                    "backend": args.backend,
                    "mention_required": args.mention_required,
                },
                "tmux": target_status,
                "target_action": (
                    "create" if target_status["state"] == "stopped" else "reuse"
                ),
                "service": args.service,
                "fixed_flow": [
                    "resolve exact endpoint",
                    "validate complete candidate",
                    "create or reuse exact-cwd tmux target",
                    "atomically write route",
                    "restart supervised bridge",
                    "verify route + tmux + service",
                    "live IM acceptance",
                ],
            }
            if not args.apply:
                print(json.dumps(plan, ensure_ascii=False, indent=2))
                print("plan only: repeat with --apply after verifying every value")
                return 0
            topic: dict[str, Any] | None = None
            created_target = False
            try:
                item = dict(item_base)
                if topic_mode == "create":
                    if args.channel == "telegram":
                        topic = create_telegram_topic(
                            env_file, args.credential, int(chat_id), str(topic_title)
                        )
                    else:
                        topic = create_feishu_topic(
                            env_file, args.credential, str(chat_id), str(topic_title)
                        )
                    item["thread_id"] = topic["thread_id"]
                    item["thread_root_message_id"] = topic["root_message_id"]
                    validate_bindings([*store.list(), binding_from_mapping(item)])
                if target_status["state"] == "stopped":
                    _preflight_target(runtime, target, cwd, create_target=True)
                    created_target = True
                _bound, verification = _apply_route_transaction(
                    args.bindings_file,
                    args.service,
                    runtime,
                    store,
                    lambda: store.bind(item),
                )
            except Exception:
                if created_target:
                    try:
                        runtime.remove_created_target(target)
                    except Exception:
                        pass
                if topic is not None:
                    try:
                        if args.channel == "telegram":
                            delete_telegram_topic(
                                env_file,
                                args.credential,
                                int(chat_id),
                                int(topic["thread_id"]),
                            )
                        else:
                            delete_feishu_message(
                                env_file,
                                args.credential,
                                str(topic["root_message_id"]),
                            )
                    except Exception:
                        pass
                raise
            verification["provisioning"] = {
                "endpoint_mode": topic_mode,
                "created_topic": topic,
                "target_action": plan["target_action"],
            }
            print(json.dumps(verification, ensure_ascii=False))
            return 0
        if args.admin_command == "install-contract":
            installed = install_admin_context(
                cwd=args.cwd,
                bindings_file=args.bindings_file,
                service=args.service,
            )
            print("installed: " + ", ".join(str(path) for path in installed))
            return 0
        if args.admin_command == "verify-context":
            verification = verify_admin_context(
                cwd=args.cwd, bindings_file=args.bindings_file, service=args.service
            )
            print(
                json.dumps(
                    verification,
                    ensure_ascii=False,
                    indent=None if args.as_json else 2,
                )
            )
            return 0 if verification["ok"] else 2
        if args.admin_command == "bind-topic":
            target = parse_tmux_target(args.tmux_target)
            cwd = _resolved_directory(args.cwd)
            item = _route_item(args, target, cwd)
            candidate = [*store.list(), binding_from_mapping(item)]
            validate_bindings(candidate)
            target_status = _preflight_target(
                runtime, target, cwd, create_target=False
            )
            plan = {
                "operation": "bind-topic",
                "apply": args.apply,
                "route": item,
                "tmux": target_status,
                "create_target": args.create_target,
                "service": args.service,
            }
            if not args.apply:
                print(json.dumps(plan, ensure_ascii=False, indent=2))
                print("plan only: repeat with --apply after verifying every value")
                return 0
            if target_status["state"] == "stopped" and not args.create_target:
                raise AdminOperationError(
                    f"tmux target {target.value} does not exist; pass --create-target explicitly"
                )
            created_target = False
            if args.create_target and target_status["state"] == "stopped":
                _preflight_target(runtime, target, cwd, create_target=True)
                created_target = True
            try:
                _bound, verification = _apply_route_transaction(
                    args.bindings_file,
                    args.service,
                    runtime,
                    store,
                    lambda: store.bind(item),
                )
            except Exception:
                if created_target:
                    try:
                        runtime.remove_created_target(target)
                    except Exception:
                        pass
                raise
            print(json.dumps(verification, ensure_ascii=False))
            return 0
        if args.admin_command == "rename-project":
            existing = store.inspect(args.name)
            old_target = parse_tmux_target(existing.tmux_target)
            new_name = args.new_name.strip()
            if not new_name or ":" in new_name:
                raise AdminOperationError("new route/session name must be non-empty and contain no colon")
            if new_name == existing.name:
                raise AdminOperationError("new route name must differ from the existing name")
            if old_target.window != 0 or old_target.pane != 0:
                raise AdminOperationError("rename-project supports only a single-session target at 0.0")
            if any(binding.name == new_name for binding in store.list()):
                raise AdminOperationError(f"route name already exists: {new_name!r}")
            if any(
                binding.tmux_session == new_name and binding.name != existing.name
                for binding in store.list()
            ):
                raise AdminOperationError(f"tmux session is already routed: {new_name!r}")
            old_cwd = existing.cwd.expanduser().resolve()
            new_cwd = args.new_cwd.expanduser().resolve()
            if old_cwd == new_cwd:
                raise AdminOperationError("new cwd must differ from the existing cwd")
            if not old_cwd.is_dir():
                raise AdminOperationError(f"existing route cwd is not a directory: {old_cwd}")
            if new_cwd.exists():
                raise AdminOperationError(f"new cwd already exists: {new_cwd}")
            if not new_cwd.parent.is_dir():
                raise AdminOperationError(f"new cwd parent is not a directory: {new_cwd.parent}")
            old_status = _preflight_target(runtime, old_target, old_cwd, create_target=False)
            if old_status["state"] != "running":
                raise AdminOperationError(f"tmux target is not running: {old_target.value}")
            session_targets = [
                item for item in runtime.list_targets()
                if str(item.get("target", "")).split(":", 1)[0] == old_target.session
            ]
            if len(session_targets) != 1 or session_targets[0].get("target") != old_target.value:
                raise AdminOperationError(
                    f"rename-project requires exactly one pane in tmux session {old_target.session!r}"
                )
            new_target = TmuxTarget(new_name, 0, 0)
            if runtime.target_status(new_target)["state"] != "stopped":
                raise AdminOperationError(f"new tmux target already exists: {new_target.value}")
            replacement_item = binding_to_mapping(existing)
            replacement_item.update(
                {
                    "name": new_name,
                    "tmux_session": new_name,
                    "cwd": str(new_cwd),
                    "provider_session_id": None,
                    "transcript_path": None,
                    "last_session_id": None,
                }
            )
            replacement = binding_from_mapping(replacement_item)
            candidate = [
                replacement if binding.name == existing.name else binding
                for binding in store.list()
            ]
            validate_bindings(candidate)
            plan = {
                "operation": "rename-project",
                "apply": args.apply,
                "before": binding_to_json(existing),
                "after": binding_to_json(replacement),
                "filesystem": {"from": str(old_cwd), "to": str(new_cwd)},
                "tmux": {"from": old_target.value, "to": new_target.value, "respawn": True},
                "preserves": ["endpoint", "backend", "mention_required"],
                "resets": ["provider_session_id", "transcript_path"],
                "service": args.service,
            }
            if not args.apply:
                print(json.dumps(plan, ensure_ascii=False, indent=2))
                print("plan only: repeat with --apply after verifying every value")
                return 0
            if args.bindings_file.is_symlink():
                raise AdminOperationError(
                    f"bindings file must not be a symbolic link: {args.bindings_file}"
                )
            original = args.bindings_file.read_bytes()
            mode = args.bindings_file.stat().st_mode & 0o777
            cwd_moved = False
            session_renamed = False
            try:
                os.replace(old_cwd, new_cwd)
                cwd_moved = True
                runtime.rename_session(old_target.session, new_name)
                session_renamed = True
                runtime.respawn_target(new_target, new_cwd)
                bound = store.replace(existing.name, replacement_item)
                store.validate()
                runtime.restart_service(args.service)
                verification = verify_route(store, bound.name, args.service, runtime)
                if not verification["ok"]:
                    raise AdminOperationError(
                        "post-apply verification failed: " + "; ".join(verification["errors"])
                    )
            except Exception:
                _restore_file(args.bindings_file, original, mode)
                if session_renamed:
                    try:
                        runtime.rename_session(new_name, old_target.session)
                        runtime.respawn_target(old_target, new_cwd if cwd_moved else old_cwd)
                    except Exception:
                        pass
                if cwd_moved:
                    try:
                        os.replace(new_cwd, old_cwd)
                        if session_renamed:
                            runtime.respawn_target(old_target, old_cwd)
                    except Exception:
                        pass
                try:
                    runtime.restart_service(args.service)
                except Exception:
                    pass
                raise
            verification["rename"] = plan
            print(json.dumps(verification, ensure_ascii=False))
            return 0
        if args.admin_command == "move-topic":
            existing = store.inspect(args.name)
            if existing.channel != args.channel:
                raise AdminOperationError(
                    f"route {args.name!r} uses channel {existing.channel!r}, not {args.channel!r}"
                )
            chat_id = _parse_identifier(args.chat_id, channel=args.channel)
            thread_id = _parse_identifier(args.thread_id, channel=args.channel)
            _require_feishu_thread_root(
                channel=args.channel,
                thread_id=thread_id,
                root_message_id=args.thread_root_message_id,
            )
            replacement_item = binding_to_mapping(existing)
            replacement_item["chat_id"] = chat_id
            replacement_item["thread_id"] = thread_id
            replacement_item["thread_root_message_id"] = args.thread_root_message_id
            replacement = binding_from_mapping(replacement_item)
            candidate = [
                replacement if binding.name == args.name else binding
                for binding in store.list()
            ]
            validate_bindings(candidate)
            plan = {
                "operation": "move-topic",
                "apply": args.apply,
                "before": binding_to_json(existing),
                "after_endpoint": {
                    "channel": args.channel,
                    "credential": existing.bot_token_env,
                    "chat_id": chat_id,
                    "thread_id": thread_id,
                    "thread_root_message_id": args.thread_root_message_id,
                },
                "preserves": [
                    "tmux_target",
                    "cwd",
                    "backend",
                    "provider_session_id",
                    "transcript_path",
                ],
                "service": args.service,
            }
            if not args.apply:
                print(json.dumps(plan, ensure_ascii=False, indent=2))
                print("plan only: repeat with --apply after verifying every value")
                return 0
            _moved, verification = _apply_route_transaction(
                args.bindings_file,
                args.service,
                runtime,
                store,
                lambda: store.replace(args.name, replacement_item),
            )
            print(json.dumps(verification, ensure_ascii=False))
            return 0
        if args.admin_command == "adopt-pi-session":
            existing = store.inspect(args.name)
            if existing.backend != "pi":
                raise AdminOperationError(
                    f"route {args.name!r} uses {existing.backend!r}, not Pi"
                )
            target = parse_tmux_target(existing.tmux_target)
            target_status = _preflight_target(
                runtime, target, existing.cwd, create_target=False
            )
            if (
                target_status["state"] != "running"
                or target_status.get("command") != "pi"
            ):
                raise AdminOperationError(
                    f"Pi target is not running at {existing.tmux_target}"
                )
            session_id, transcript = _pi_session_identity(
                args.session_file, existing.cwd
            )
            replacement_item = binding_to_mapping(existing)
            replacement_item["provider_session_id"] = session_id
            replacement_item["transcript_path"] = str(transcript)
            replacement = binding_from_mapping(replacement_item)
            candidate = [
                replacement if binding.name == args.name else binding
                for binding in store.list()
            ]
            validate_bindings(candidate)
            plan = {
                "operation": "adopt-pi-session",
                "apply": args.apply,
                "route": args.name,
                "tmux": target_status,
                "before": {
                    "provider_session_id": existing.provider_session_id,
                    "transcript_path": (
                        str(existing.transcript_path)
                        if existing.transcript_path else None
                    ),
                },
                "after": {
                    "provider_session_id": session_id,
                    "transcript_path": str(transcript),
                },
                "service": args.service,
            }
            if not args.apply:
                print(json.dumps(plan, ensure_ascii=False, indent=2))
                print("plan only: repeat with --apply after verifying every value")
                return 0
            _adopted, verification = _apply_route_transaction(
                args.bindings_file,
                args.service,
                runtime,
                store,
                lambda: store.replace(args.name, replacement_item),
            )
            verification["session_adoption"] = plan["after"]
            print(json.dumps(verification, ensure_ascii=False))
            return 0
        if args.admin_command == "verify":
            result = verify_route(store, args.name, args.service, runtime)
            if args.as_json:
                print(json.dumps(result, ensure_ascii=False))
            else:
                print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result["ok"] else 2
    except (AdminOperationError, ConfigValidationError, KeyError, OSError, RuntimeError) as exc:
        message = f"route not found: {exc.args[0]}" if isinstance(exc, KeyError) else str(exc)
        print(message)
        return 2
    return 2
