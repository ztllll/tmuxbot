import asyncio
import json
from pathlib import Path

from tmuxbot.backends.claude_code import ClaudeCodeBackend
from tmuxbot.core.events import TerminalState, TerminalStatus
from tmuxbot.core.replies import ReplyEnvelope
from tmuxbot.frontends.feishu import FeishuFrontend
from tmuxbot.state import Binding


def test_feishu_assistant_reply_returns_editable_message_and_provider_footer(tmp_path):
    sent = []
    frontend = FeishuFrontend.__new__(FeishuFrontend)
    frontend.backend = ClaudeCodeBackend()
    frontend._outbound_message_ids = set()
    frontend._send_card_sync = lambda chat_id, md: sent.append((chat_id, md)) or "om_123"
    b = Binding(
        name="alpha",
        chat_id="oc_123",
        thread_id=None,
        tmux_session="alpha",
        tmux_window=0,
        tmux_pane=0,
        cwd=Path(tmp_path),
        channel="feishu",
    )
    envelope = ReplyEnvelope(
        title="回复",
        body="已完成",
        footer=TerminalStatus(
            state=TerminalState.IDLE,
            label="ready",
            model="claude-opus-4-7",
            context_used=12_000,
            context_limit=200_000,
        ),
    )

    result = asyncio.run(frontend.send_assistant_reply(b, envelope))

    assert result.message_id == "om_123"
    card = json.loads(sent[0][1])
    assert card["schema"] == "2.0"
    assert "已完成" in card["body"]["elements"][0]["content"]
    assert "claude-opus-4-7" in sent[0][1]
    assert "12k/200k" in sent[0][1]


def test_feishu_assistant_reply_promotes_relative_image_without_exposing_path(tmp_path):
    image = tmp_path / "chart.png"
    image.write_bytes(b"png")
    sent = []
    frontend = FeishuFrontend.__new__(FeishuFrontend)
    frontend.backend = ClaudeCodeBackend()
    frontend._outbound_message_ids = set()
    frontend._send_card_sync = lambda chat_id, md: sent.append(("card", chat_id, md)) or "om_124"

    async def send_image(chat_id, thread_id, path, caption=None):
        sent.append(("image", chat_id, Path(path), caption))

    frontend.send_image = send_image
    frontend.send_file = send_image
    b = Binding(
        name="alpha",
        chat_id="oc_123",
        thread_id=None,
        tmux_session="alpha",
        tmux_window=0,
        tmux_pane=0,
        cwd=Path(tmp_path),
        channel="feishu",
    )

    result = asyncio.run(
        frontend.send_assistant_reply(
            b,
            ReplyEnvelope(title="回复", body="图表：![趋势](<./chart.png>)"),
        )
    )

    assert result.message_id == "om_124"
    assert str(image) not in sent[0][2]
    assert "图表：趋势" in json.loads(sent[0][2])["body"]["elements"][0]["content"]
    assert sent[1] == ("image", "oc_123", image, "chart.png")


def test_feishu_assistant_reply_falls_back_to_legacy_card_when_v2_send_fails(tmp_path):
    sent = []
    frontend = FeishuFrontend.__new__(FeishuFrontend)
    frontend.backend = ClaudeCodeBackend()
    frontend._outbound_message_ids = set()

    def send_card(chat_id, content):
        sent.append(content)
        if json.loads(content).get("schema") == "2.0":
            return None
        return "om_legacy"

    frontend._send_card_sync = send_card
    b = Binding(
        name="alpha",
        chat_id="oc_123",
        thread_id=None,
        tmux_session="alpha",
        tmux_window=0,
        tmux_pane=0,
        cwd=Path(tmp_path),
        channel="feishu",
    )

    result = asyncio.run(
        frontend.send_assistant_reply(b, ReplyEnvelope(title="回复", body="兼容内容"))
    )

    assert result.message_id == "om_legacy"
    assert json.loads(sent[0])["schema"] == "2.0"
    assert "schema" not in json.loads(sent[1])
    assert "兼容内容" in sent[1]
