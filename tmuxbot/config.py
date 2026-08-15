"""配置加载: .env (TG_BOT_TOKEN, BOSS_USER_ID) + bindings.yaml → State"""
from __future__ import annotations

import logging
import os
import tempfile
import threading
from pathlib import Path

import yaml
from dotenv import load_dotenv

from tmuxbot.paths import default_admin_cwd
from tmuxbot.state import Binding, S
from tmuxbot.utils import load_offsets
from tmuxbot.validation import ConfigValidationError, validate_bindings

log = logging.getLogger("tmuxbot")
_BINDINGS_WRITE_LOCK = threading.Lock()
_TRUE_VALUES = {"1", "true", "yes", "on"}


def _admin_binding(boss_user_id: int, bindings_file: Path) -> Binding | None:
    if os.getenv("TMUXBOT_ADMIN_ENABLED", "").strip().lower() not in _TRUE_VALUES:
        return None
    channel = os.getenv("TMUXBOT_ADMIN_CHANNEL", "telegram").strip().lower()
    backend = os.getenv("TMUXBOT_ADMIN_CLI", "").strip()
    if backend not in {"claude_code", "codex", "pi"}:
        raise ConfigValidationError(
            ["TMUXBOT_ADMIN_CLI must be one of: claude_code, codex, pi"]
        )
    raw_chat_id = os.getenv("TMUXBOT_ADMIN_CHAT_ID", "").strip()
    if channel == "telegram":
        if not raw_chat_id:
            raw_chat_id = str(boss_user_id)
        if not raw_chat_id.lstrip("-").isdigit():
            raise ConfigValidationError(["TMUXBOT_ADMIN_CHAT_ID must be an integer for telegram"])
        chat_id: int | str = int(raw_chat_id)
        if chat_id <= 0:
            raise ConfigValidationError(
                ["TMUXBOT_ADMIN_CHAT_ID must be a positive private user id for telegram"]
            )
        credential = os.getenv("TMUXBOT_ADMIN_CREDENTIAL", "TG_BOT_TOKEN").strip()
    elif channel == "feishu":
        if not raw_chat_id:
            raise ConfigValidationError(
                ["TMUXBOT_ADMIN_CHAT_ID is required for feishu admin DM"]
            )
        chat_id = raw_chat_id
        credential = os.getenv("TMUXBOT_ADMIN_CREDENTIAL", "FEISHU").strip()
    else:
        raise ConfigValidationError(
            ["TMUXBOT_ADMIN_CHANNEL must be telegram or feishu"]
        )
    tmux_session = os.getenv("TMUXBOT_ADMIN_TMUX", "tmuxbot-admin").strip()
    if not tmux_session:
        raise ConfigValidationError(["TMUXBOT_ADMIN_TMUX must not be empty"])
    configured_cwd = os.getenv("TMUXBOT_ADMIN_CWD", "").strip()
    cwd = (
        Path(configured_cwd).expanduser().resolve()
        if configured_cwd
        else default_admin_cwd(os.environ)
    )
    if cwd == Path.home().expanduser().resolve():
        raise ConfigValidationError(
            [
                "TMUXBOT_ADMIN_CWD must not be the user home directory; "
                "use a dedicated private workspace such as "
                f"{default_admin_cwd(os.environ)}"
            ]
        )
    try:
        cwd.mkdir(parents=True, exist_ok=True, mode=0o700)
        if cwd.is_symlink() or not cwd.is_dir():
            raise OSError("path is not a real directory")
        os.chmod(cwd, 0o700)
        from tmuxbot.admin_cli import install_admin_context

        install_admin_context(
            cwd=cwd,
            bindings_file=bindings_file,
            service=os.getenv("TMUXBOT_SERVICE", "tmuxbot.service").strip()
            or "tmuxbot.service",
        )
    except (OSError, RuntimeError) as exc:
        raise ConfigValidationError(
            [f"TMUXBOT_ADMIN_CWD must be a prepared private directory: {cwd}: {exc}"]
        ) from exc
    return Binding(
        name="tmuxbot-admin",
        chat_id=chat_id,
        thread_id=None,
        tmux_session=tmux_session,
        tmux_window=0,
        tmux_pane=0,
        cwd=cwd,
        backend=backend,
        bot_token_env=credential,
        channel=channel,
        mention_required=False,
        admin=True,
    )


def save_binding_identity(bindings_file: Path | None, binding: Binding) -> None:
    """把运行时确认的 provider 身份与稳定通道路由元数据写回 bindings.yaml。"""
    if bindings_file is None:
        return
    try:
        with _BINDINGS_WRITE_LOCK:
            raw = yaml.safe_load(bindings_file.read_text(encoding="utf-8")) or {}
            entries = raw.setdefault("bindings", [])
            entry = next(
                (candidate for candidate in entries if candidate.get("name") == binding.name),
                None,
            )
            if entry is None and binding.admin:
                entry = {
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
                    "mention_required": binding.mention_required,
                    "admin": True,
                    "thread_root_message_id": binding.thread_root_message_id,
                }
                entries.append(entry)
            if entry is None:
                log.warning(
                    "[%s] binding 不在 %s, 无法持久化会话身份",
                    binding.name,
                    bindings_file,
                )
                return
            if binding.admin:
                entry.update(
                    {
                        "channel": binding.channel,
                        "bot_token_env": binding.bot_token_env,
                        "chat_id": binding.chat_id,
                        "thread_id": binding.thread_id,
                        "tmux_session": binding.tmux_session,
                        "tmux_window": binding.tmux_window,
                        "tmux_pane": binding.tmux_pane,
                        "cwd": str(binding.cwd),
                        "backend": binding.backend,
                        "mention_required": binding.mention_required,
                        "admin": True,
                    }
                )
            if binding.thread_root_message_id:
                entry["thread_root_message_id"] = binding.thread_root_message_id
            else:
                entry.pop("thread_root_message_id", None)
            if binding.provider_session_id:
                entry["provider_session_id"] = binding.provider_session_id
            else:
                entry.pop("provider_session_id", None)
            if binding.transcript_path:
                entry["transcript_path"] = str(binding.transcript_path)
            else:
                entry.pop("transcript_path", None)
            rendered = yaml.safe_dump(raw, allow_unicode=True, sort_keys=False)
            temp_path: Path | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w",
                    encoding="utf-8",
                    dir=bindings_file.parent,
                    prefix=f".{bindings_file.name}.",
                    suffix=".tmp",
                    delete=False,
                ) as handle:
                    handle.write(rendered)
                    handle.flush()
                    os.fsync(handle.fileno())
                    temp_path = Path(handle.name)
                os.chmod(temp_path, 0o600)
                os.replace(temp_path, bindings_file)
            finally:
                if temp_path is not None and temp_path.exists():
                    temp_path.unlink()
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
            # chat_id 兼容 Telegram (int) 和飞书 (str: oc_xxx)。
            # 能转 int 就转 (Telegram); 否则保留 str (飞书)。
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
                    admin=bool(b.get("admin", False)),
                    thread_root_message_id=(
                        str(b.get("thread_root_message_id"))
                        if b.get("thread_root_message_id")
                        else None
                    ),
                    provider_session_id=provider_session_id,
                    transcript_path=Path(transcript_raw) if transcript_raw else None,
                    last_session_id=provider_session_id,
                )
            )
    except (KeyError, TypeError, ValueError) as exc:
        raise ConfigValidationError([f"invalid binding entry: {exc}"]) from exc

    persisted_admins = [binding for binding in bindings if binding.admin]
    bindings = [binding for binding in bindings if not binding.admin]
    admin = _admin_binding(boss_user_id, bindings_file)
    if admin is not None:
        if len(persisted_admins) > 1:
            raise ConfigValidationError(
                ["bindings.yaml contains multiple persisted admin identity records"]
            )
        persisted_admin = persisted_admins[0] if persisted_admins else None
        if persisted_admin is not None:
            same_target = (
                persisted_admin.tmux_target == admin.tmux_target
                and persisted_admin.cwd.expanduser().resolve() == admin.cwd
                and persisted_admin.backend == admin.backend
            )
            if same_target:
                admin.provider_session_id = persisted_admin.provider_session_id
                admin.last_session_id = persisted_admin.last_session_id
                admin.transcript_path = persisted_admin.transcript_path
        bindings.append(admin)

    offsets = load_offsets(offsets_file)
    validate_bindings(bindings, require_nonempty=not allow_empty_bindings)
    S.boss_user_id = boss_user_id
    S.setup_mode = setup_mode
    S.bindings = bindings
    S.offsets = offsets
