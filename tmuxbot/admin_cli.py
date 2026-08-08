"""Deterministic Admin operations for exact IM topic routes.

The Admin LLM supplies intent parameters.  This module owns preflight checks,
validated atomic route writes, supervised bridge restart, rollback, and runtime
verification so callers do not need to reproduce deployment mechanics.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import tempfile
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from dotenv import dotenv_values

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
        if result.returncode != 0:
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
    if len(parts) != 4 or parts[0] != "c":
        raise AdminOperationError(
            "expected a private forum message link in the exact form "
            "https://t.me/c/<internal-chat-id>/<thread-id>/<message-id>"
        )
    internal_chat_id, thread_id, message_id = parts[1:]
    if not all(part.isdigit() and int(part) > 0 for part in parts[1:]):
        raise AdminOperationError("Telegram link chat/thread/message identifiers must be positive integers")
    return {
        "channel": "telegram",
        "chat_id": int(f"-100{internal_chat_id}"),
        "thread_id": int(thread_id),
        "message_id": int(message_id),
        "message_link": value.strip(),
    }


def discover_feishu_topics(
    env_file: Path,
    credential: str,
    chat_id: str,
    *,
    limit: int = 50,
) -> list[dict[str, Any]]:
    values = dotenv_values(env_file)
    app_id = values.get(f"{credential}_APP_ID") or os.getenv(f"{credential}_APP_ID")
    app_secret = values.get(f"{credential}_APP_SECRET") or os.getenv(
        f"{credential}_APP_SECRET"
    )
    if not app_id or not app_secret:
        raise AdminOperationError(
            f"missing {credential}_APP_ID/{credential}_APP_SECRET in {env_file}"
        )

    def request_json(
        url: str,
        *,
        body: Mapping[str, Any] | None = None,
        token: str | None = None,
    ) -> dict[str, Any]:
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        request = urllib.request.Request(url, data=data, headers=headers)
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

    auth = request_json(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        body={"app_id": app_id, "app_secret": app_secret},
    )
    token = str(auth.get("tenant_access_token") or "")
    if not token:
        raise AdminOperationError("Feishu authentication returned no tenant token")
    query = urllib.parse.urlencode(
        {
            "container_id_type": "chat",
            "container_id": chat_id,
            "sort_type": "ByCreateTimeDesc",
            "page_size": max(1, min(limit, 50)),
        }
    )
    payload = request_json(
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

You manage exact IM endpoint -> tmux pane routes. Use tmuxbot's deterministic
Admin commands; do not reconstruct YAML/systemd/tmux transactions by hand.

Canonical objects:
- endpoint = channel + credential + chat_id + thread_id
- target = tmux SESSION:WINDOW.PANE + cwd
- adapter = claude_code | codex | pi
- route = one exact endpoint mapped to one exact target and adapter

Hard rules:
1. Never guess a topic/thread ID. If it is unavailable, ask for the ID or a
   message link. Never create a new topic unless the user explicitly asks.
2. A group root and every topic/thread are different endpoints. Unbound endpoints
   stay silent and must not touch tmux.
3. Run the plan form first. Use --apply only after all displayed values match the
   request. After apply, run `tmuxbot admin verify ROUTE`.
4. Do not reuse a pane with a different cwd. Do not bind two routes to one pane.
5. Do not run `tmux kill-server`. Moving a route preserves its provider identity.
6. Project topics normally use `--mention-required false`.
7. Never create a tmux target before exact endpoint discovery, inventory review,
   and a valid bind plan. Do not run `tmux new-session` directly for a route;
   new sessions must be created transactionally by repeating the reviewed plan
   with `bind-topic --create-target --apply`.
8. Config changes are complete only after validated atomic write, supervised
   bridge restart, and verification. The Admin command owns those steps.

Deployment:
- bindings: {bindings_file}
- supervised bridge: {service}

Required workflow:
```bash
tmuxbot admin --file {bindings_file} --service {service} inventory --json
# For Telegram private forums, parse an exact message link instead of guessing:
tmuxbot admin --file {bindings_file} --service {service} telegram-topic \\
  --message-link https://t.me/c/INTERNAL_CHAT_ID/THREAD_ID/MESSAGE_ID --json
# For Feishu, discover exact existing topics instead of guessing:
tmuxbot admin --file {bindings_file} --service {service} feishu-topics \\
  --env-file {env_file} --credential FEISHU_CODEX --chat-id oc_xxx --json

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

tmuxbot admin --file {bindings_file} --service {service} verify ROUTE --json
```

If a required value is missing or ambiguous, stop and ask the operator. A safe
refusal is correct; guessing a route is not.
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
    install = subparsers.add_parser(
        "install-contract", help="install/update managed AGENTS.md and CLAUDE.md blocks"
    )
    install.add_argument("--cwd", type=Path, default=Path.home())

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
        if args.admin_command == "install-contract":
            cwd = args.cwd.expanduser().resolve()
            if not cwd.is_dir():
                raise AdminOperationError(f"contract cwd must be an existing directory: {cwd}")
            block = _managed_contract_block(
                bindings_file=args.bindings_file, service=args.service
            )
            installed = [cwd / "AGENTS.md", cwd / "CLAUDE.md"]
            for path in installed:
                _install_managed_block(path, block)
            print("installed: " + ", ".join(str(path) for path in installed))
            return 0
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
