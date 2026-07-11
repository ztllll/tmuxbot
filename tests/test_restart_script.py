from pathlib import Path


def test_restart_script_prefers_installed_systemd_service():
    script = Path("bin/restart.sh").read_text(encoding="utf-8")

    assert "systemctl --user cat tmuxbot.service" in script
    assert "systemctl --user restart tmuxbot.service" in script
    assert 'pkill -KILL -f "python3 tmuxbot.py"' not in script
