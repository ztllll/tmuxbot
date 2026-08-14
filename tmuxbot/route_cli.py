"""Deterministic route inspection and atomic YAML editing commands."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Any, Mapping, Sequence

import yaml

from tmuxbot.state import Binding
from tmuxbot.validation import ConfigValidationError, validate_bindings


def binding_from_mapping(item: Mapping[str, Any]) -> Binding:
    raw_chat_id = item.get("chat_id", 0)
    chat_id: int | str = (
        int(raw_chat_id) if str(raw_chat_id).lstrip("-").isdigit() else str(raw_chat_id)
    )
    provider_session_id = item.get("provider_session_id") or item.get("last_session_id")
    transcript = item.get("transcript_path")
    return Binding(
        name=str(item.get("name", "")),
        chat_id=chat_id,
        thread_id=item.get("thread_id"),
        tmux_session=str(item.get("tmux_session", "")),
        tmux_window=int(item.get("tmux_window", 0)),
        tmux_pane=int(item.get("tmux_pane", 0)),
        cwd=Path(str(item.get("cwd", ""))),
        backend=str(item.get("backend", "claude_code")),
        bot_token_env=str(item.get("bot_token_env", "TG_BOT_TOKEN")),
        channel=str(item.get("channel", "telegram")),
        mention_required=item.get("mention_required"),
        admin=bool(item.get("admin", False)),
        thread_root_message_id=(
            str(item.get("thread_root_message_id")) if item.get("thread_root_message_id") else None
        ),
        provider_session_id=str(provider_session_id) if provider_session_id else None,
        transcript_path=Path(str(transcript)) if transcript else None,
        last_session_id=str(provider_session_id) if provider_session_id else None,
    )


def binding_to_mapping(binding: Binding) -> dict[str, Any]:
    item: dict[str, Any] = {
        "name": binding.name,
        "channel": binding.channel,
        "bot_token_env": binding.bot_token_env,
        "chat_id": binding.chat_id,
        "thread_id": binding.thread_id,
        "tmux_session": binding.tmux_session,
        "tmux_window": binding.tmux_window,
        "tmux_pane": binding.tmux_pane,
        "cwd": str(binding.cwd),
        "backend": binding.backend,
    }
    if binding.mention_required is not None:
        item["mention_required"] = binding.mention_required
    if binding.admin:
        item["admin"] = True
    if binding.thread_root_message_id:
        item["thread_root_message_id"] = binding.thread_root_message_id
    if binding.provider_session_id:
        item["provider_session_id"] = binding.provider_session_id
    if binding.transcript_path:
        item["transcript_path"] = str(binding.transcript_path)
    return item


def binding_to_json(binding: Binding) -> dict[str, Any]:
    payload = asdict(binding)
    payload["cwd"] = str(binding.cwd)
    payload["transcript_path"] = str(binding.transcript_path) if binding.transcript_path else None
    payload["tmux_target"] = binding.tmux_target
    payload.pop("pending_session_handoff_after", None)
    return payload


class RouteStore:
    """Small validated seam around the human-readable route YAML."""

    def __init__(self, path: Path) -> None:
        self.path = path.expanduser()

    def _read_document(self) -> tuple[dict[str, Any], list[Binding]]:
        if not self.path.is_file():
            raise ConfigValidationError([f"bindings file does not exist: {self.path}"])
        try:
            raw = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as exc:
            raise ConfigValidationError([f"invalid bindings YAML: {exc}"]) from exc
        if not isinstance(raw, dict):
            raise ConfigValidationError(["bindings YAML root must be a mapping"])
        entries = raw.get("bindings", [])
        if not isinstance(entries, list):
            raise ConfigValidationError(["bindings must be a list"])
        try:
            bindings = [binding_from_mapping(item) for item in entries]
        except (TypeError, ValueError) as exc:
            raise ConfigValidationError([f"invalid binding entry: {exc}"]) from exc
        validate_bindings(bindings, require_nonempty=False)
        return raw, bindings

    def list(self) -> list[Binding]:
        _, bindings = self._read_document()
        return bindings

    def inspect(self, name: str) -> Binding:
        for binding in self.list():
            if binding.name == name:
                return binding
        raise KeyError(name)

    def validate(self) -> int:
        return len(self.list())

    def bind(self, item: Mapping[str, Any]) -> Binding:
        raw, bindings = self._read_document()
        binding = binding_from_mapping(item)
        candidate = [*bindings, binding]
        validate_bindings(candidate)
        raw["bindings"] = [binding_to_mapping(existing) for existing in candidate]
        self._atomic_write(raw)
        return binding

    def replace(self, name: str, item: Mapping[str, Any]) -> Binding:
        raw, bindings = self._read_document()
        if not any(binding.name == name for binding in bindings):
            raise KeyError(name)
        replacement = binding_from_mapping(item)
        candidate = [replacement if binding.name == name else binding for binding in bindings]
        validate_bindings(candidate)
        raw["bindings"] = [binding_to_mapping(binding) for binding in candidate]
        self._atomic_write(raw)
        return replacement

    def move_endpoint(
        self,
        name: str,
        *,
        chat_id: int | str,
        thread_id: int | str | None,
    ) -> Binding:
        existing = self.inspect(name)
        item = binding_to_mapping(existing)
        item["chat_id"] = chat_id
        item["thread_id"] = thread_id
        return self.replace(name, item)

    def unbind(self, name: str) -> Binding:
        raw, bindings = self._read_document()
        removed = next((binding for binding in bindings if binding.name == name), None)
        if removed is None:
            raise KeyError(name)
        candidate = [binding for binding in bindings if binding.name != name]
        validate_bindings(candidate, require_nonempty=False)
        raw["bindings"] = [binding_to_mapping(binding) for binding in candidate]
        self._atomic_write(raw)
        return removed

    def _atomic_write(self, document: Mapping[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.is_symlink():
            raise ConfigValidationError([f"bindings file must not be a symbolic link: {self.path}"])
        rendered = yaml.safe_dump(dict(document), allow_unicode=True, sort_keys=False)
        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                delete=False,
            ) as stream:
                stream.write(rendered)
                stream.flush()
                os.fsync(stream.fileno())
                temp_path = Path(stream.name)
            os.chmod(temp_path, 0o600)
            os.replace(temp_path, self.path)
        finally:
            if temp_path is not None and temp_path.exists():
                temp_path.unlink()


def build_route_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tmuxbot route")
    parser.add_argument("--file", type=Path, required=True, dest="bindings_file")
    subparsers = parser.add_subparsers(dest="route_command", required=True)

    list_parser = subparsers.add_parser("list")
    list_parser.add_argument("--json", action="store_true", dest="as_json")

    inspect_parser = subparsers.add_parser("inspect")
    inspect_parser.add_argument("name")
    inspect_parser.add_argument("--json", action="store_true", dest="as_json")

    subparsers.add_parser("validate")

    bind_parser = subparsers.add_parser("bind")
    bind_parser.add_argument("--name", required=True)
    bind_parser.add_argument("--channel", choices=("telegram", "feishu"), required=True)
    bind_parser.add_argument("--credential", required=True, dest="bot_token_env")
    bind_parser.add_argument("--chat-id", required=True)
    bind_parser.add_argument("--thread-id")
    bind_parser.add_argument("--thread-root-message-id")
    bind_parser.add_argument("--tmux-session", required=True)
    bind_parser.add_argument("--window", type=int, default=0, dest="tmux_window")
    bind_parser.add_argument("--pane", type=int, default=0, dest="tmux_pane")
    bind_parser.add_argument("--cwd", required=True)
    bind_parser.add_argument("--backend", choices=("claude_code", "codex", "omp"), required=True)
    mention = bind_parser.add_mutually_exclusive_group()
    mention.add_argument("--mention-required", action="store_true", dest="mention_required")
    mention.add_argument("--no-mention-required", action="store_false", dest="mention_required")
    bind_parser.set_defaults(mention_required=None)

    unbind_parser = subparsers.add_parser("unbind")
    unbind_parser.add_argument("name")
    return parser


def _parse_identifier(value: str | None, *, channel: str) -> int | str | None:
    if value is None or value.lower() in {"none", "null", ""}:
        return None
    if channel == "telegram":
        try:
            return int(value)
        except ValueError as exc:
            raise ConfigValidationError(["telegram chat_id/thread_id must be integers"]) from exc
    return value


def run_route_command(argv: Sequence[str]) -> int:
    args = build_route_parser().parse_args(list(argv))
    store = RouteStore(args.bindings_file)
    try:
        if args.route_command == "list":
            routes = store.list()
            if args.as_json:
                print(json.dumps([binding_to_json(item) for item in routes], ensure_ascii=False))
            else:
                for item in routes:
                    print(
                        f"{item.name}\t{item.channel}:{item.bot_token_env}:"
                        f"{item.chat_id}:{item.thread_id}\t{item.tmux_target}\t"
                        f"{item.backend}\t{item.cwd}"
                    )
            return 0
        if args.route_command == "inspect":
            item = store.inspect(args.name)
            if args.as_json:
                print(json.dumps(binding_to_json(item), ensure_ascii=False))
            else:
                print(
                    yaml.safe_dump(
                        binding_to_mapping(item), allow_unicode=True, sort_keys=False
                    ).rstrip()
                )
            return 0
        if args.route_command == "validate":
            count = store.validate()
            print(f"valid: {count} route{'s' if count != 1 else ''}")
            return 0
        if args.route_command == "bind":
            channel = args.channel
            item = {
                "name": args.name,
                "channel": channel,
                "bot_token_env": args.bot_token_env,
                "chat_id": _parse_identifier(args.chat_id, channel=channel),
                "thread_id": _parse_identifier(args.thread_id, channel=channel),
                "thread_root_message_id": args.thread_root_message_id,
                "tmux_session": args.tmux_session,
                "tmux_window": args.tmux_window,
                "tmux_pane": args.tmux_pane,
                "cwd": args.cwd,
                "backend": args.backend,
                "mention_required": args.mention_required,
            }
            bound = store.bind(item)
            print(f"bound: {bound.name}")
            return 0
        if args.route_command == "unbind":
            removed = store.unbind(args.name)
            print(f"unbound: {removed.name}")
            return 0
    except ConfigValidationError as exc:
        print(str(exc))
        return 2
    except KeyError as exc:
        print(f"route not found: {exc.args[0]}")
        return 3
    return 2
