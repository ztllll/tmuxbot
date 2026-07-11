"""Runtime filesystem locations for source checkouts and installed packages."""
from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


def _path(environ: Mapping[str, str], name: str) -> Path | None:
    value = environ.get(name)
    return Path(value).expanduser() if value else None


@dataclass(frozen=True, slots=True)
class RuntimePaths:
    config_dir: Path
    data_dir: Path
    state_dir: Path
    env_file: Path
    bindings_file: Path
    database_file: Path
    offsets_file: Path
    lock_file: Path
    hook_spool_file: Path

    @classmethod
    def discover(
        cls,
        environ: Mapping[str, str],
        *,
        home: Path | None = None,
        legacy_project_dir: Path | None = None,
    ) -> "RuntimePaths":
        resolved_home = (home if home is not None else Path.home()).expanduser()
        config_base = _path(environ, "XDG_CONFIG_HOME") or resolved_home / ".config"
        data_base = _path(environ, "XDG_DATA_HOME") or resolved_home / ".local/share"
        state_base = _path(environ, "XDG_STATE_HOME") or resolved_home / ".local/state"
        config_dir = _path(environ, "TMUXBOT_CONFIG_DIR") or config_base / "tmuxbot"
        legacy_data_dir = _path(environ, "TMUXBOT_DATA_DIR")
        data_dir = legacy_data_dir or data_base / "tmuxbot"
        state_dir = (
            _path(environ, "TMUXBOT_STATE_DIR")
            or legacy_data_dir
            or state_base / "tmuxbot"
        )

        env_file = _path(environ, "TMUXBOT_ENV")
        bindings_file = _path(environ, "TMUXBOT_BINDINGS")
        if legacy_project_dir is not None:
            legacy_project_dir = legacy_project_dir.expanduser()
            legacy_env = legacy_project_dir / ".env"
            legacy_bindings = legacy_project_dir / "bindings.yaml"
            if env_file is None and legacy_env.is_file():
                env_file = legacy_env
            if bindings_file is None and legacy_bindings.is_file():
                bindings_file = legacy_bindings

        return cls(
            config_dir=config_dir,
            data_dir=data_dir,
            state_dir=state_dir,
            env_file=env_file or config_dir / ".env",
            bindings_file=bindings_file or config_dir / "bindings.yaml",
            database_file=_path(environ, "TMUXBOT_DATABASE")
            or data_dir / "control-plane.sqlite3",
            offsets_file=_path(environ, "TMUXBOT_OFFSETS") or state_dir / "offsets.json",
            lock_file=_path(environ, "TMUXBOT_LOCK") or state_dir / "tmuxbot.lock",
            hook_spool_file=_path(environ, "TMUXBOT_HOOK_SPOOL")
            or state_dir / "claude-hooks.jsonl",
        )

    def ensure_private_directories(self) -> None:
        for directory in (self.config_dir, self.data_dir, self.state_dir):
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            if directory.is_symlink():
                raise OSError(f"runtime directory must not be a symbolic link: {directory}")
            if not directory.is_dir():
                raise OSError(f"runtime path is not a directory: {directory}")
            os.chmod(directory, 0o700)
