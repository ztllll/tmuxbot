import asyncio
import json
from pathlib import Path

import pytest

from tmuxbot.frontends.feishu_streaming import (
    FeishuStreamingSession,
    StreamingPrefixError,
)
from tmuxbot.frontends.feishu import FeishuFrontend
from tmuxbot.core.events import TerminalState, TerminalStatus
from tmuxbot.state import Binding


def test_feishu_streaming_session_throttles_prefix_updates_and_closes():
    calls = []
    now = [10.0]

    async def sleep(delay):
        calls.append(("sleep", delay))
        now[0] += delay

    async def update(card_id, element_id, content, sequence):
        calls.append(("update", card_id, element_id, content, sequence))
        return True

    async def close(card_id, card, sequence):
        calls.append(("close", card_id, card, sequence))
        return True

    session = FeishuStreamingSession(
        card_id="card-1",
        element_id="reply_body_0",
        update_content=update,
        close_card=close,
        clock=lambda: now[0],
        sleep=sleep,
    )

    async def run():
        assert await session.append("正在")
        assert await session.append("正在检查")
        assert await session.close({"schema": "2.0"})

    asyncio.run(run())

    assert calls == [
        ("update", "card-1", "reply_body_0", "正在", 1),
        ("sleep", 0.2),
        ("update", "card-1", "reply_body_0", "正在检查", 2),
        ("close", "card-1", {"schema": "2.0"}, 3),
    ]
    assert session.closed


def test_feishu_streaming_session_rejects_non_prefix_or_updates_after_close():
    async def update(*args):
        return True

    async def close(*args):
        return True

    session = FeishuStreamingSession(
        card_id="card-1",
        element_id="reply_body_0",
        update_content=update,
        close_card=close,
    )

    async def run():
        await session.append("abc")
        with pytest.raises(StreamingPrefixError):
            await session.append("abd")
        await session.close({"schema": "2.0"})
        assert await session.append("abcd") is False

    asyncio.run(run())


def test_feishu_streaming_session_disables_itself_after_api_failure():
    async def update(*args):
        return False

    async def close(*args):
        return False

    session = FeishuStreamingSession(
        card_id="card-1",
        element_id="reply_body_0",
        update_content=update,
        close_card=close,
    )

    async def run():
        assert await session.append("abc") is False
        assert session.failed
        assert await session.append("abcd") is False
        assert await session.close({"schema": "2.0"}) is False

    asyncio.run(run())


def test_feishu_frontend_uses_cardkit_stream_and_closes_without_actions(tmp_path):
    calls = []
    frontend = FeishuFrontend.__new__(FeishuFrontend)
    frontend.streaming_enabled = True
    frontend._streaming_cards = {}
    frontend._outbound_message_ids = set()
    frontend._v2_message_ids = set()
    frontend._create_streaming_card_sync = lambda chat_id, content: (
        calls.append(("create", chat_id, json.loads(content))) or ("card-1", "om-1")
    )
    frontend._stream_card_content_sync = lambda card_id, element_id, content, sequence: (
        calls.append(("update", card_id, element_id, content, sequence)) or True
    )
    frontend._close_streaming_card_sync = lambda card_id, card, sequence: (
        calls.append(("close", card_id, card, sequence)) or True
    )
    b = Binding(
        name="alpha",
        chat_id="oc_alpha",
        thread_id=None,
        tmux_session="alpha",
        tmux_window=0,
        tmux_pane=0,
        cwd=Path(tmp_path),
        backend="codex",
        channel="feishu",
    )

    async def run():
        message = await frontend.send_reply_stream_start(b, "正在")
        await frontend.edit_reply_stream(b, message.message_id, "正在检查")
        await frontend.edit_reply_stream(b, message.message_id, "检查完成", final=True)

    asyncio.run(run())

    assert calls[0][0:2] == ("create", "oc_alpha")
    assert calls[0][2]["config"]["streaming_mode"] is True
    assert calls[0][2]["header"]["template"] == "yellow"
    assert calls[1][0:4] == ("update", "card-1", "reply_body_0", "正在检查")
    assert calls[2][0:2] == ("close", "card-1")
    assert calls[2][2]["config"]["streaming_mode"] is False
    assert calls[2][2]["header"]["template"] == "green"
    assert not any(item["tag"] == "button" for item in calls[2][2]["body"]["elements"])
    assert frontend._streaming_cards == {}


def test_feishu_status_card_keeps_working_color_when_edited(tmp_path):
    cards = []
    b = Binding(
        name="alpha",
        chat_id="oc_alpha",
        thread_id=None,
        tmux_session="alpha",
        tmux_window=0,
        tmux_pane=0,
        cwd=Path(tmp_path),
        backend="codex",
        channel="feishu",
    )
    frontend = FeishuFrontend.__new__(FeishuFrontend)
    frontend.bindings = [b]
    frontend.card_v2_enabled = True
    frontend._outbound_message_ids = set()
    frontend._v2_message_ids = set()
    frontend._v2_message_states = {}
    frontend._send_card_sync = lambda chat_id, content: (
        cards.append(("send", json.loads(content))) or "om-1"
    )
    frontend._patch_card_sync = lambda message_id, content: (
        cards.append(("edit", json.loads(content))) or True
    )

    async def run():
        message = await frontend.send_status_html(
            b.chat_id,
            b.thread_id,
            "工作中",
            display_state="working",
            footer=TerminalStatus(state=TerminalState.WORKING, model="gpt-5.6-terra"),
        )
        await frontend.edit_html(b.chat_id, message.message_id, "仍在工作")
        await frontend.finalize_status_html(b.chat_id, message.message_id, "任务完成")

    asyncio.run(run())

    assert cards[0][1]["header"]["template"] == "yellow"
    assert cards[1][1]["header"]["template"] == "yellow"
    assert cards[2][1]["header"]["template"] == "green"
    assert cards[2][1]["header"]["title"]["content"].startswith("已完成")
    assert "gpt-5.6-terra" in json.dumps(cards[0][1], ensure_ascii=False)
    assert "gpt-5.6-terra" in json.dumps(cards[1][1], ensure_ascii=False)
