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
Environment=TMUXBOT_BRIDGE_PID_FILE=%t/tmuxbot/bridge.pid
ExecStop=/bin/sh -c 'if [ -r "$TMUXBOT_BRIDGE_PID_FILE" ]; then kill -TERM "$(cat "$TMUXBOT_BRIDGE_PID_FILE")" 2>/dev/null || true; fi'
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
    refresh_service = unit_dir / "tmuxbot-bridge-refresh@.service"
    refresh_service.write_text(
        "[Unit]\nDescription=Refresh tmuxbot channel bridge %i\n\n"
        "[Service]\nType=oneshot\n"
        "ExecStart=/usr/bin/systemctl --user restart %i.service\n",
        encoding="utf-8",
    )
    refresh_timer = unit_dir / "tmuxbot-bridge-refresh@.timer"
    refresh_timer.write_text(
        "[Unit]\nDescription=Refresh tmuxbot channel bridge %i every six hours\n\n"
        "[Timer]\nOnBootSec=30min\nOnUnitInactiveSec=6h\nPersistent=true\n"
        "Unit=tmuxbot-bridge-refresh@%i.service\n\n"
        "[Install]\nWantedBy=timers.target\n",
        encoding="utf-8",
    )
    os.chmod(refresh_service, 0o600)
    os.chmod(refresh_timer, 0o600)
    runner(["systemctl", "--user", "daemon-reload"])
    if start_now:
        runner(["systemctl", "--user", "enable", "--now", "tmuxbot.service"])
        runner(
            [
                "systemctl", "--user", "enable", "--now",
                "tmuxbot-bridge-refresh@tmuxbot.timer",
            ]
        )
    return unit_path
