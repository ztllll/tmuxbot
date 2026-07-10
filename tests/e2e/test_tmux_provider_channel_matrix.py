import asyncio
import json
import shutil
import subprocess
import sys
import time
import uuid

import pytest

from tmuxbot.backends.claude_code import ClaudeCodeBackend
from tmuxbot.backends.codex import CodexBackend
from tmuxbot.core.events import ProviderEventKind
from tmuxbot.core.replies import ReplyEnvelope
from tmuxbot.state import Binding
from tmuxbot.tmux import tmux_capture, tmux_kill_session, tmux_send_text


pytestmark = pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is required")


class FakeChannel:
    def __init__(self, name: str) -> None:
        self.name = name
        self.received: list[ReplyEnvelope] = []

    async def send_assistant_reply(self, _binding: Binding, envelope: ReplyEnvelope):
        self.received.append(envelope)
        return type("Message", (), {"message_id": f"{self.name}-1"})()


def _wait_for(target: str, needle: str, timeout: float = 3.0) -> str:
    deadline = time.monotonic() + timeout
    captured = ""
    while time.monotonic() < deadline:
        captured = tmux_capture(target, 80)
        if needle in captured:
            return captured
        time.sleep(0.05)
    raise AssertionError(f"{needle!r} not found in tmux capture: {captured!r}")


def _fake_transcript(provider: str, text: str) -> str:
    if provider == "claude":
        return json.dumps(
            {
                "type": "assistant",
                "uuid": "fake-claude-message",
                "message": {"content": [{"type": "text", "text": text}]},
            },
            ensure_ascii=False,
        )
    return json.dumps(
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "id": "fake-codex-message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text}],
            },
        },
        ensure_ascii=False,
    )


@pytest.mark.parametrize("provider", ["claude", "codex"])
@pytest.mark.parametrize("channel_name", ["telegram", "feishu"])
def test_tmux_provider_channel_matrix(tmp_path, provider, channel_name):
    script = tmp_path / "fake_cli.py"
    script.write_text(
        "import sys\n"
        "provider = sys.argv[1]\n"
        "print(f'READY[{provider}]', flush=True)\n"
        "for line in sys.stdin:\n"
        "    text = line.rstrip('\\n')\n"
        "    print(f'REPLY[{provider}]:{text}', flush=True)\n",
        encoding="utf-8",
    )
    session = f"tmuxbot-e2e-{provider}-{uuid.uuid4().hex[:8]}"
    target = f"{session}:0.0"
    subprocess.run(
        [
            "tmux",
            "new-session",
            "-d",
            "-s",
            session,
            "-c",
            str(tmp_path),
            sys.executable,
            str(script),
            provider,
        ],
        check=True,
    )
    try:
        _wait_for(target, f"READY[{provider}]")
        asyncio.run(tmux_send_text(target, "first"))
        asyncio.run(tmux_send_text(target, "second"))
        capture = _wait_for(target, f"REPLY[{provider}]:second")
        assert capture.index(f"REPLY[{provider}]:first") < capture.index(
            f"REPLY[{provider}]:second"
        )

        backend = ClaudeCodeBackend() if provider == "claude" else CodexBackend()
        status_capture = (
            "claude-opus-4-7 12k/200k tokens bypass permissions"
            if provider == "claude"
            else f"model: gpt-5\neffort: high\ndirectory: {tmp_path}\nYOLO mode"
        )
        status = backend.parse_terminal_status(status_capture)
        events = backend.parse_event(
            _fake_transcript(provider, "matrix complete"),
            provider_session_id=f"{provider}-session",
        )
        final = next(event for event in events if event.kind == ProviderEventKind.FINAL_TEXT)
        envelope = ReplyEnvelope(title="回复", body=final.text, footer=status)
        channel = FakeChannel(channel_name)
        binding = Binding(
            name=f"matrix-{provider}-{channel_name}",
            chat_id="oc_matrix" if channel_name == "feishu" else 123,
            thread_id=None,
            tmux_session=session,
            tmux_window=0,
            tmux_pane=0,
            cwd=tmp_path,
            backend="claude_code" if provider == "claude" else "codex",
            channel=channel_name,
        )

        result = asyncio.run(channel.send_assistant_reply(binding, envelope))

        assert result.message_id == f"{channel_name}-1"
        assert channel.received == [envelope]
        assert channel.received[0].body == "matrix complete"
        assert backend.format_status_footer(channel.received[0].footer)
    finally:
        tmux_kill_session(session)
