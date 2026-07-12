from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path


def _run(argv: list[str]) -> None:
    subprocess.run(argv, check=True)


def install_service(
    *,
    home: Path | None = None,
    executable: Path | None = None,
    start_now: bool = False,
    runner: Callable[[list[str]], None] = _run,
) -> Path:
    resolved_home = (home or Path.home()).expanduser()
    if executable is None:
        candidate = shutil.which("tmuxbot")
        if candidate is None:
            raise RuntimeError("未找到 tmuxbot 可执行文件；请先安装 tmuxbot[full]")
        executable = Path(candidate).resolve()
    unit_dir = resolved_home / ".config/systemd/user"
    unit_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(unit_dir, 0o700)
    unit_path = unit_dir / "tmuxbot.service"
    env_file = resolved_home / ".config/tmuxbot/.env"
    content = f"""[Unit]
Description=tmuxbot WebUI and tmux bridge supervisor
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
# tmux panes outlive bridge/WebUI deploys; only stop the supervisor itself.
KillMode=process
EnvironmentFile=-{env_file}
ExecStart={executable} serve
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
"""
    temp = unit_path.with_suffix(".service.tmp")
    temp.write_text(content, encoding="utf-8")
    os.chmod(temp, 0o600)
    os.replace(temp, unit_path)
    runner(["systemctl", "--user", "daemon-reload"])
    if start_now:
        runner(["systemctl", "--user", "enable", "--now", "tmuxbot.service"])
    return unit_path
