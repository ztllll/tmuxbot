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
    self_heal: bool = False,
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
    lifecycle_environment = (
        "Environment=TMUXBOT_LIFECYCLE_ENABLED=1\n" if self_heal else ""
    )
    content = f"""[Unit]
Description=tmuxbot WebUI and tmux bridge supervisor
After=network-online.target
Wants=network-online.target
# A transient crash must never leave the user bridge permanently rate-limited.
StartLimitIntervalSec=0

[Service]
Type=simple
# Existing tmux panes outlive deploys. With lifecycle enabled, missing panes and
# provider TUIs are recreated from their persisted route/session identities.
KillMode=process
EnvironmentFile=-{env_file}
Environment=TMUXBOT_BRIDGE_PID_FILE=%t/tmuxbot/bridge.pid
{lifecycle_environment}ExecStop=/bin/sh -c 'if [ -r "$TMUXBOT_BRIDGE_PID_FILE" ]; then kill -TERM "$(cat "$TMUXBOT_BRIDGE_PID_FILE")" 2>/dev/null || true; fi'
ExecStart={executable} serve
Restart=always
RestartSec=5
TimeoutStopSec=15
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
"""
    temp = unit_path.with_suffix(".service.tmp")
    temp.write_text(content, encoding="utf-8")
    os.chmod(temp, 0o600)
    os.replace(temp, unit_path)

    # Releases before the single-service model installed a periodic forced-restart
    # timer. The bridge and each channel now own their reconnect loops, so keeping
    # that helper would create avoidable outages and violate the one-service contract.
    refresh_service = unit_dir / "tmuxbot-bridge-refresh@.service"
    refresh_timer = unit_dir / "tmuxbot-bridge-refresh@.timer"
    refresh_link = (
        unit_dir / "timers.target.wants/tmuxbot-bridge-refresh@tmuxbot.timer"
    )
    if any(path.exists() or path.is_symlink() for path in (
        refresh_service, refresh_timer, refresh_link
    )):
        runner(
            [
                "systemctl",
                "--user",
                "disable",
                "--now",
                "tmuxbot-bridge-refresh@tmuxbot.timer",
            ]
        )
    for path in (refresh_link, refresh_service, refresh_timer):
        path.unlink(missing_ok=True)

    runner(["systemctl", "--user", "daemon-reload"])
    if start_now:
        runner(["systemctl", "--user", "enable", "--now", "tmuxbot.service"])
    return unit_path
