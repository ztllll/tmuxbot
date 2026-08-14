from pathlib import Path

from tmuxbot.service_install import install_service


def test_install_service_writes_single_self_healing_user_unit(tmp_path: Path) -> None:
    calls: list[list[str]] = []
    unit = install_service(
        home=tmp_path,
        executable=Path("/opt/tmuxbot/bin/tmuxbot"),
        start_now=True,
        self_heal=True,
        runner=lambda argv: calls.append(argv),
    )
    content = unit.read_text(encoding="utf-8")
    assert "ExecStart=/opt/tmuxbot/bin/tmuxbot serve" in content
    assert "EnvironmentFile=-" in content
    assert "Environment=TMUXBOT_LIFECYCLE_ENABLED=1" in content
    assert "Environment=TMUXBOT_LIFECYCLE_INTERVAL=3600" in content
    assert "StartLimitIntervalSec=0" in content
    assert "Restart=always" in content
    assert "KillMode=process" in content
    assert "TMUXBOT_BRIDGE_OMPD_FILE=%t/tmuxbot/bridge.pid" in content
    assert "ExecStop=/bin/sh -c" in content
    assert "token" not in content.lower()
    assert calls == [
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", "--now", "tmuxbot.service"],
    ]
    assert not (tmp_path / ".config/systemd/user/tmuxbot-bridge-refresh@.service").exists()
    assert not (tmp_path / ".config/systemd/user/tmuxbot-bridge-refresh@.timer").exists()


def test_install_service_removes_legacy_refresh_helper(tmp_path: Path) -> None:
    unit_dir = tmp_path / ".config/systemd/user"
    wants_dir = unit_dir / "timers.target.wants"
    wants_dir.mkdir(parents=True)
    refresh_service = unit_dir / "tmuxbot-bridge-refresh@.service"
    refresh_timer = unit_dir / "tmuxbot-bridge-refresh@.timer"
    refresh_service.write_text("legacy", encoding="utf-8")
    refresh_timer.write_text("legacy", encoding="utf-8")
    refresh_link = wants_dir / "tmuxbot-bridge-refresh@tmuxbot.timer"
    refresh_link.symlink_to(refresh_timer)
    calls: list[list[str]] = []

    install_service(
        home=tmp_path,
        executable=Path("/opt/tmuxbot/bin/tmuxbot"),
        runner=lambda argv: calls.append(argv),
    )

    assert calls == [
        [
            "systemctl",
            "--user",
            "disable",
            "--now",
            "tmuxbot-bridge-refresh@tmuxbot.timer",
        ],
        ["systemctl", "--user", "daemon-reload"],
    ]
    assert not refresh_service.exists()
    assert not refresh_timer.exists()
    assert not refresh_link.exists()
