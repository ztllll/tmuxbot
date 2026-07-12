import json
from pathlib import Path

import pytest

from tmuxbot.command_adapter import binding_token
from tmuxbot.core.events import TerminalState, TerminalStatus
from tmuxbot.core.replies import ReplyEnvelope
from tmuxbot.core.rich_messages import build_reply_document, render_telegram_document
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


def test_build_feishu_card_v2_has_structured_header_summary_body_and_no_buttons(tmp_path):
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
    assert card["header"]["title"]["content"] == f"回复 · {tmp_path.name}"
    assert card["header"]["subtitle"]["content"] == "alpha"
    assert card["header"]["template"] == "yellow"
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
    assert not any(element["tag"] == "note" for element in elements)
    status = next(element for element in elements if element["element_id"] == "reply_status")
    assert status["tag"] == "div"
    assert status["text"]["text_size"] == "notation"
    assert status["text"]["text_color"] == "grey"

    assert not any(element["tag"] == "button" for element in elements)


def test_channel_headers_share_project_and_session_identity(tmp_path):
    project = tmp_path / "project-alpha"
    project.mkdir()
    b = binding(project)
    document = build_reply_document(
        b,
        ReplyEnvelope(
            title="tmuxbot",
            body="正在分析问题",
            metadata={"display_state": "working"},
        ),
    )

    card = build_feishu_card_v2(document, binding_token(b.name))
    telegram = render_telegram_document(document, full_output_threshold=None)

    assert card["header"]["title"]["content"] == "工作中 · project-alpha"
    assert card["header"]["subtitle"]["content"] == "alpha"
    assert "工作中 · project-alpha" in telegram.chat_html
    assert "alpha" in telegram.chat_html


@pytest.mark.parametrize(
    ("display_state", "template"),
    [
        ("working", "yellow"),
        ("waiting", "orange"),
        ("completed", "green"),
        ("idle", "green"),
        ("blocked", "red"),
        ("dead", "red"),
        ("error", "red"),
        ("info", "blue"),
        ("unknown", "grey"),
    ],
)
def test_build_feishu_card_v2_maps_display_state_to_header_color(
    tmp_path, display_state, template
):
    b = binding(tmp_path)
    document = build_reply_document(
        b,
        ReplyEnvelope(
            title="回复",
            body="内容",
            metadata={"display_state": display_state},
        ),
    )

    card = build_feishu_card_v2(document, binding_token(b.name))

    assert card["header"]["template"] == template


def test_build_feishu_streaming_card_is_working_yellow(tmp_path):
    b = binding(tmp_path)
    document = build_reply_document(b, ReplyEnvelope(title="回复", body="正在处理"))

    card = build_feishu_card_v2(document, binding_token(b.name), streaming=True)

    assert card["header"]["template"] == "yellow"


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
