import json
from pathlib import Path

import pytest

from tmuxbot.command_adapter import binding_token
from tmuxbot.core.events import TerminalState, TerminalStatus
from tmuxbot.core.replies import ReplyEnvelope
from tmuxbot.core.rich_messages import build_reply_document
from tmuxbot.frontends.feishu_cards import (
    FeishuCardTooLarge,
    build_feishu_card_v2,
    serialize_feishu_card,
)
from tmuxbot.state import Binding


def binding(tmp_path: Path) -> Binding:
    return Binding(
        name="alpha",
        chat_id="oc_1",
        thread_id=None,
        tmux_session="alpha",
        tmux_window=0,
        tmux_pane=0,
        cwd=tmp_path,
        backend="claude_code",
        channel="feishu",
    )


def test_build_feishu_card_v2_has_structured_header_summary_body_footer_and_buttons(tmp_path):
    b = binding(tmp_path)
    document = build_reply_document(
        b,
        ReplyEnvelope(
            title="回复",
            body="## 结论\n\n完成 <b>部署</b>。\n\n```bash\necho ok\n```",
            footer=TerminalStatus(state=TerminalState.WORKING, model="claude-opus"),
            actions=("screen", "status", "cancel", "interrupt"),
        ),
        footer_text="claude-opus · Working",
    )

    card = build_feishu_card_v2(document, binding_token(b.name))

    assert card["schema"] == "2.0"
    assert card["config"]["update_multi"] is True
    assert card["config"]["width_mode"] == "fill"
    assert card["config"]["summary"]["content"].startswith("结论 完成 部署")
    assert card["header"]["title"]["content"] == "回复"
    assert card["header"]["subtitle"]["content"] == "alpha"
    assert card["header"]["template"] == "blue"
    tags = [item["text"]["content"] for item in card["header"]["text_tag_list"]]
    assert tags == ["claude_code"]

    elements = card["body"]["elements"]
    assert all(element.get("element_id", "x").replace("_", "").isalnum() for element in elements)
    markdown = "\n".join(
        element.get("content", "") for element in elements if element["tag"] == "markdown"
    )
    assert "## 结论" in markdown
    assert "完成 **部署**。" in markdown
    assert "```bash\necho ok\n```" in markdown
    assert any(element["tag"] == "note" for element in elements)

    buttons = [element for element in elements if element["tag"] == "button"]
    assert [button["text"]["content"] for button in buttons] == [
        "屏幕",
        "状态",
        "取消",
        "强制中断",
    ]
    assert [button["behaviors"][0]["value"]["action"] for button in buttons] == [
        "refresh",
        "status",
        "esc",
        "confirm_ctrl_c",
    ]
    assert all(
        button["behaviors"][0]["value"]["token"] == binding_token(b.name)
        for button in buttons
    )


def test_build_feishu_interrupt_confirmation_card_uses_confirmed_action(tmp_path):
    b = binding(tmp_path)
    document = build_reply_document(
        b,
        ReplyEnvelope(title="确认中断", body="这会向 tmux 发送 Ctrl-C。"),
    )

    card = build_feishu_card_v2(
        document,
        binding_token(b.name),
        confirm_interrupt=True,
    )

    buttons = [item for item in card["body"]["elements"] if item["tag"] == "button"]
    assert [button["behaviors"][0]["value"]["action"] for button in buttons] == [
        "ctrl_c",
        "refresh",
    ]
    assert buttons[0]["type"] == "danger"


def test_serialize_feishu_card_enforces_utf8_byte_limit(tmp_path):
    b = binding(tmp_path)
    document = build_reply_document(
        b,
        ReplyEnvelope(title="回复", body="中文" * 100),
    )
    card = build_feishu_card_v2(document, binding_token(b.name))

    serialized = serialize_feishu_card(card, max_bytes=30_000)
    assert json.loads(serialized)["schema"] == "2.0"

    with pytest.raises(FeishuCardTooLarge):
        serialize_feishu_card(card, max_bytes=100)
