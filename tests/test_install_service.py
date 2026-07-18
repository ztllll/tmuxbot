from pathlib import Path

from tmuxbot.service_install import install_service


def test_install_service_writes_user_unit_without_secrets(tmp_path: Path) -> None:
    calls: list[list[str]] = []
    unit = install_service(
        home=tmp_path,
        executable=Path("/opt/tmuxbot/bin/tmuxbot"),
        start_now=True,
        runner=lambda argv: calls.append(argv),
    )
    content = unit.read_text(encoding="utf-8")
    assert "ExecStart=/opt/tmuxbot/bin/tmuxbot serve" in content
    assert "EnvironmentFile=-" in content
    assert "KillMode=process" in content
    assert "TMUXBOT_BRIDGE_PID_FILE=%t/tmuxbot/bridge.pid" in content
    assert "ExecStop=/bin/sh -c" in content
    assert "token" not in content.lower()
    assert calls == [
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", "--now", "tmuxbot.service"],
        [
            "systemctl", "--user", "enable", "--now",
            "tmuxbot-bridge-refresh@tmuxbot.timer",
        ],
    ]
    assert (tmp_path / ".config/systemd/user/tmuxbot-bridge-refresh@.service").exists()
    assert (tmp_path / ".config/systemd/user/tmuxbot-bridge-refresh@.timer").exists()
