"""配置加载: .env (TG_BOT_TOKEN, BOSS_USER_ID) + bindings.yaml → State"""
from __future__ import annotations

import logging
import os
from pathlib import Path

import yaml
from dotenv import load_dotenv

from tmuxbot.state import Binding, S
from tmuxbot.utils import load_offsets

log = logging.getLogger("tmuxbot")


def load_config(env_file: Path, bindings_file: Path, offsets_file: Path) -> None:
    """读 .env + bindings.yaml + offsets.json → 填充 S 单例"""
    load_dotenv(env_file)
    S.boss_user_id = int(os.getenv("BOSS_USER_ID", "0") or "0")
    S.setup_mode = S.boss_user_id == 0
    raw = yaml.safe_load(bindings_file.read_text()) or {}
    S.bindings = []
    for b in raw.get("bindings", []):
        S.bindings.append(
            Binding(
                name=b["name"],
                chat_id=int(b.get("chat_id", 0)),
                thread_id=b.get("thread_id"),
                tmux_session=b["tmux_session"],
                tmux_window=int(b.get("tmux_window", 0)),
                tmux_pane=int(b.get("tmux_pane", 0)),
                cwd=Path(b["cwd"]),
                backend=b.get("backend", "claude_code"),
                bot_token_env=b.get("bot_token_env", "TG_BOT_TOKEN"),
                idle_kill_seconds=int(b.get("idle_kill_seconds", 0) or 0),
            )
        )
    S.offsets = load_offsets(offsets_file)
