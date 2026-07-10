import asyncio
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
    assert "已完成" in sent[0][1]
    assert "claude-opus-4-7" in sent[0][1]
    assert "12k/200k" in sent[0][1]
