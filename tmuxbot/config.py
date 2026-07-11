"""配置加载: .env (TG_BOT_TOKEN, BOSS_USER_ID) + bindings.yaml → State"""
from __future__ import annotations

import logging
import os
import tempfile
import threading
from pathlib import Path

import yaml
from dotenv import load_dotenv

from tmuxbot.state import Binding, S
from tmuxbot.utils import load_offsets
from tmuxbot.validation import ConfigValidationError, validate_bindings

log = logging.getLogger("tmuxbot")
_BINDINGS_WRITE_LOCK = threading.Lock()


def save_binding_identity(bindings_file: Path | None, binding: Binding) -> None:
    """把运行时确认的 provider 会话身份写回 bindings.yaml。"""
    if bindings_file is None:
        return
    try:
        with _BINDINGS_WRITE_LOCK:
            raw = yaml.safe_load(bindings_file.read_text(encoding="utf-8")) or {}
            for entry in raw.get("bindings", []):
                if entry.get("name") != binding.name:
                    continue
                if binding.provider_session_id:
                    entry["provider_session_id"] = binding.provider_session_id
                else:
                    entry.pop("provider_session_id", None)
                if binding.transcript_path:
                    entry["transcript_path"] = str(binding.transcript_path)
                else:
                    entry.pop("transcript_path", None)
                rendered = yaml.safe_dump(raw, allow_unicode=True, sort_keys=False)
                with tempfile.NamedTemporaryFile(
                    mode="w",
                    encoding="utf-8",
                    dir=bindings_file.parent,
                    prefix=f".{bindings_file.name}.",
                    suffix=".tmp",
                    delete=False,
                ) as handle:
                    handle.write(rendered)
                    temp_path = Path(handle.name)
                os.replace(temp_path, bindings_file)
                return
            log.warning(
                "[%s] binding 不在 %s, 无法持久化会话身份",
                binding.name,
                bindings_file,
            )
    except Exception:
        log.exception("[%s] 持久化 provider 会话身份失败", binding.name)


def load_config(
    env_file: Path,
    bindings_file: Path,
    offsets_file: Path,
    *,
    allow_missing_bindings: bool = False,
    allow_empty_bindings: bool = False,
) -> None:
    """读 .env + bindings.yaml + offsets.json → 填充 S 单例"""
    load_dotenv(env_file, override=False)
    try:
        boss_user_id = int(os.getenv("BOSS_USER_ID", "0") or "0")
    except ValueError as exc:
        raise ConfigValidationError(["BOSS_USER_ID must be an integer"]) from exc
    setup_mode = boss_user_id == 0

    if not bindings_file.is_file():
        if not allow_missing_bindings:
            raise ConfigValidationError(
                [f"bindings file does not exist: {bindings_file}"]
            )
        raw: object = {"bindings": []}
    else:
        try:
            raw = yaml.safe_load(bindings_file.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            raise ConfigValidationError([f"invalid bindings YAML: {exc}"]) from exc
        if raw is None:
            raw = {}
    if not isinstance(raw, dict):
        raise ConfigValidationError(["bindings YAML root must be a mapping"])
    entries = raw.get("bindings", [])
    if not isinstance(entries, list):
        raise ConfigValidationError(["bindings must be a list"])

    bindings: list[Binding] = []
    try:
        for b in entries:
            if not isinstance(b, dict):
                raise TypeError("each binding must be a mapping")
        # chat_id 兼容 Telegram (int) 和飞书 (str: oc_xxx)
        # 能转 int 就转 (Telegram); 否则保留 str (飞书)
            cid_raw = b.get("chat_id", 0)
            chat_id: int | str = (
                int(cid_raw) if str(cid_raw).lstrip("-").isdigit() else str(cid_raw)
            )
            provider_session_id = b.get("provider_session_id") or b.get("last_session_id")
            transcript_raw = b.get("transcript_path")
            bindings.append(
                Binding(
                    name=b["name"],
                    chat_id=chat_id,
                    thread_id=b.get("thread_id"),
                    tmux_session=b["tmux_session"],
                    tmux_window=int(b.get("tmux_window", 0)),
                    tmux_pane=int(b.get("tmux_pane", 0)),
                    cwd=Path(b["cwd"]),
                    backend=b.get("backend", "claude_code"),
                    bot_token_env=b.get("bot_token_env", "TG_BOT_TOKEN"),
                    channel=b.get("channel", "telegram"),
                    mention_required=b.get("mention_required"),
                    provider_session_id=provider_session_id,
                    transcript_path=Path(transcript_raw) if transcript_raw else None,
                    last_session_id=provider_session_id,
                )
            )
    except (KeyError, TypeError, ValueError) as exc:
        raise ConfigValidationError([f"invalid binding entry: {exc}"]) from exc

    offsets = load_offsets(offsets_file)
    validate_bindings(bindings, require_nonempty=not allow_empty_bindings)
    S.boss_user_id = boss_user_id
    S.setup_mode = setup_mode
    S.bindings = bindings
    S.offsets = offsets
