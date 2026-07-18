from pathlib import Path
from types import SimpleNamespace
import asyncio
import json

from tmuxbot.command_adapter import binding_token
from tmuxbot.frontends.feishu import FeishuFrontend
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
        backend="codex",
        channel="feishu",
    )


def event(b: Binding, action: str, *, open_id: str = "ou_boss", chat_id: str | None = None):
    return SimpleNamespace(
        event=SimpleNamespace(
            operator=SimpleNamespace(open_id=open_id),
            action=SimpleNamespace(
                value={"token": binding_token(b.name), "action": action}
            ),
            context=SimpleNamespace(open_chat_id=chat_id or str(b.chat_id)),
        )
    )


def frontend(b: Binding):
    instance = FeishuFrontend.__new__(FeishuFrontend)
    instance.bindings = [b]
    instance.boss_open_ids = {"ou_boss"}
    instance.backend = SimpleNamespace(format_status_footer=lambda status: None)
    instance._outbound_message_ids = set()
    instance.bindings_file = None
    instance.group_only_when_mentioned = True
    scheduled = []
    instance._schedule_card_action = lambda binding, chat_id, action: scheduled.append(
        (binding, chat_id, action)
    )
    return instance, scheduled


def test_feishu_card_action_validates_and_schedules_tmux_action(tmp_path):
    b = binding(tmp_path)
    instance, scheduled = frontend(b)

    response = instance._on_card_action(event(b, "refresh"))

    assert response.toast.type == "success"
    assert scheduled == [(b, "oc_alpha", "refresh")]


def test_feishu_card_action_rejects_unauthorized_or_wrong_chat(tmp_path):
    b = binding(tmp_path)
    instance, scheduled = frontend(b)

    unauthorized = instance._on_card_action(event(b, "refresh", open_id="ou_other"))
    wrong_chat = instance._on_card_action(event(b, "refresh", chat_id="oc_other"))

    assert unauthorized.toast.type == "error"
    assert wrong_chat.toast.type == "error"
    assert scheduled == []


def test_feishu_interrupt_action_returns_confirmation_card_before_ctrl_c(tmp_path):
    b = binding(tmp_path)
    instance, scheduled = frontend(b)

    response = instance._on_card_action(event(b, "confirm_ctrl_c"))

    assert response.toast.type == "warning"
    assert response.card.type == "raw"
    buttons = [
        item for item in response.card.data["body"]["elements"] if item["tag"] == "button"
    ]
    assert [button["behaviors"][0]["value"]["action"] for button in buttons] == [
        "ctrl_c",
        "refresh",
    ]
    assert scheduled == []


def test_feishu_card_action_rejects_malformed_values(tmp_path):
    b = binding(tmp_path)
    instance, scheduled = frontend(b)
    malformed = SimpleNamespace(
        event=SimpleNamespace(
            operator=SimpleNamespace(open_id="ou_boss"),
            action=SimpleNamespace(value="bad"),
            context=SimpleNamespace(open_chat_id="oc_alpha"),
        )
    )

    response = instance._on_card_action(malformed)

    assert response.toast.type == "error"
    assert scheduled == []


def test_feishu_capabilities_do_not_advertise_persistent_actions():
    assert not FeishuFrontend.capabilities.supports_actions


def test_feishu_panel_updates_mention_policy_and_returns_refreshed_card(tmp_path):
    b = binding(tmp_path)
    b.mention_required = True
    instance, scheduled = frontend(b)

    response = instance._on_card_action(event(b, "mention_on"))

    assert response.toast.type == "success"
    assert b.mention_required is False
    assert response.card.data["header"]["title"]["content"] == "tmuxbot 控制面板"
    assert scheduled == []


def test_feishu_panel_model_action_schedules_native_model_command(tmp_path):
    b = binding(tmp_path)
    instance, scheduled = frontend(b)

    response = instance._on_card_action(event(b, "cmd_model"))

    assert response.toast.type == "success"
    assert scheduled == [(b, "oc_alpha", "cmd_model")]


def test_feishu_forwarded_interactive_card_is_dispatched_to_tmux(tmp_path, monkeypatch):
    b = binding(tmp_path)
    instance = FeishuFrontend.__new__(FeishuFrontend)
    instance.bindings = [b]
    instance.boss_open_ids = {"ou_boss"}
    instance.group_only_when_mentioned = False
    instance.bot_open_id = "ou_bot"
    instance._outbound_message_ids = set()
    instance.bot_token_env = "FEISHU"
    instance.backend = SimpleNamespace(name="codex")
    from tmuxbot.state import State

    instance.state = State()
    instance.state.channel_health.register(
        instance.health_id, channel="feishu", credential_scope="FEISHU", binding_count=1
    )
    delivered: list[str] = []

    async def dispatch(_frontend, _backend, _binding, _state, _chat_id, _thread_id, text):
        delivered.append(text)

    monkeypatch.setattr("tmuxbot.dispatch.dispatch_incoming_text", dispatch)
    card = {
        "schema": "2.0",
        "header": {"title": {"content": "任务：发布"}},
        "body": {"elements": [{"tag": "markdown", "content": "部署并验证。"}]},
    }
    data = SimpleNamespace(
        event=SimpleNamespace(
            message=SimpleNamespace(
                chat_id="oc_alpha",
                chat_type="p2p",
                message_type="interactive",
                message_id="om_card",
                content=json.dumps(card, ensure_ascii=False),
                mentions=[],
                parent_id=None,
                root_id=None,
                reply_to_message_id=None,
            ),
            sender=SimpleNamespace(sender_id=SimpleNamespace(open_id="ou_boss")),
        )
    )

    asyncio.run(instance._handle_message(data))

    assert delivered == ["任务：发布\n部署并验证。"]
