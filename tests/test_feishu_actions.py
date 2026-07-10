from pathlib import Path
from types import SimpleNamespace

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


def test_feishu_capabilities_advertise_native_actions():
    assert FeishuFrontend.capabilities.supports_actions
