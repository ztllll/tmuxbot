import asyncio
import json
from pathlib import Path

from tmuxbot.command_adapter import binding_token
from tmuxbot.frontends.feishu import FeishuFrontend
from tmuxbot.frontends.feishu_cards import (
    build_feishu_control_panel,
    build_feishu_interaction_card,
)
from tmuxbot.state import Binding


def binding(tmp_path: Path) -> Binding:
    return Binding(
        name="alpha",
        chat_id="oc_alpha",
        thread_id=None,
        tmux_session="alpha",
        tmux_window=0,
        tmux_pane=0,
        cwd=tmp_path,
        backend="claude_code",
        channel="feishu",
        mention_required=True,
    )


def test_feishu_control_panel_is_chinese_and_contains_common_actions(tmp_path):
    b = binding(tmp_path)
    card = build_feishu_control_panel(
        "控制面板中文说明\n原生 /model 选择器",
        binding_token(b.name),
    )

    assert card["schema"] == "2.0"
    assert card["header"]["title"]["content"] == "tmuxbot 控制面板"
    markdown = [e for e in card["body"]["elements"] if e["tag"] == "markdown"]
    assert "控制面板中文说明" in markdown[0]["content"]
    buttons = [e for e in card["body"]["elements"] if e["tag"] == "button"]
    labels = [button["text"]["content"] for button in buttons]
    actions = [button["behaviors"][0]["value"]["action"] for button in buttons]
    assert "无需 @" in labels
    assert "必须 @" in labels
    assert "新会话" in labels
    assert "切换模型" in labels
    assert "重启 CLI" in labels
    assert "cmd_model" in actions
    new_button = next(button for button in buttons if button["text"]["content"] == "新会话")
    assert new_button["confirm"]["title"]["content"] == "确认创建新会话？"
    restart_button = next(
        button for button in buttons if button["text"]["content"] == "重启 CLI"
    )
    assert restart_button["confirm"]["title"]["content"] == "确认重启 CLI？"


def test_feishu_interaction_card_has_remote_tui_controls(tmp_path):
    card = build_feishu_interaction_card("模型选择器", binding_token("alpha"))
    buttons = [e for e in card["body"]["elements"] if e["tag"] == "button"]
    actions = [button["behaviors"][0]["value"]["action"] for button in buttons]

    assert actions == ["up", "left", "enter", "right", "down", "esc", "refresh"]


def test_feishu_claude_model_interaction_has_session_only_control():
    card = build_feishu_interaction_card(
        "模型选择器",
        binding_token("alpha"),
        session_model=True,
    )
    buttons = [e for e in card["body"]["elements"] if e["tag"] == "button"]
    labels = [button["text"]["content"] for button in buttons]
    actions = [button["behaviors"][0]["value"]["action"] for button in buttons]

    assert "仅本会话" in labels
    assert "model_session" in actions


def test_feishu_send_control_panel_sends_card_json_v2(tmp_path):
    calls = []
    b = binding(tmp_path)
    frontend = FeishuFrontend.__new__(FeishuFrontend)
    frontend.group_only_when_mentioned = True
    frontend._outbound_message_ids = set()
    frontend._v2_message_ids = set()
    frontend._send_card_sync = lambda chat_id, content: (
        calls.append((chat_id, json.loads(content))) or "om-panel"
    )

    message = asyncio.run(frontend.send_control_panel(b, b.chat_id, None))

    assert message.message_id == "om-panel"
    assert calls[0][1]["header"]["title"]["content"] == "tmuxbot 控制面板"
    body = [item for item in calls[0][1]["body"]["elements"] if item["tag"] == "markdown"]
    assert "当前模型" in body[0]["content"]
