import asyncio
from types import SimpleNamespace

from tmuxbot.web.control_mode import ControlModeTerminal


def test_control_mode_sends_one_hex_key_command_per_raw_byte(monkeypatch):
    terminal = ControlModeTerminal(99, object(), "alpha:0", SimpleNamespace(pane_id="%7"))
    commands = []

    async def command(value):
        commands.append(value)

    monkeypatch.setattr(terminal, "_command", command)
    asyncio.run(terminal.write(b"A\r"))

    assert commands == ["send-keys -t %7 -H 41", "send-keys -t %7 Enter"]
