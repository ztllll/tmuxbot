from pathlib import Path
from types import SimpleNamespace

import asyncio
import json

from tmuxbot.backends.pi import PiBackend
from tmuxbot.frontends.feishu import FeishuFrontend, FeishuThreadAnchorMissing
from tmuxbot.state import Binding


def frontend() -> FeishuFrontend:
    instance = FeishuFrontend.__new__(FeishuFrontend)
    instance.bindings = [
        Binding(
            name="topic-a",
            chat_id="oc_group",
            thread_id="omt_a",
            tmux_session="topic-a",
            tmux_window=0,
            tmux_pane=0,
            cwd=Path("/tmp/topic-a"),
            backend="pi",
            channel="feishu",
            bot_token_env="FEISHU",
        )
    ]
    instance.backend = PiBackend()
    instance.backends = {"pi": instance.backend}
    instance._thread_reply_anchors = {}
    instance._outbound_message_ids = set()
    instance._outbound_routes = {}
    return instance


def test_feishu_find_binding_uses_exact_string_thread():
    instance = frontend()

    assert instance.find_binding("oc_group", "omt_a").name == "topic-a"
    assert instance.find_binding("oc_group", None) is None
    assert instance.find_binding("oc_group", "omt_other") is None


def test_feishu_thread_reply_uses_authenticated_inbound_root_anchor(monkeypatch):
    instance = frontend()
    instance.app_id = "app"
    instance.app_secret = "secret"
    instance._lark = SimpleNamespace()
    instance._remember_thread_anchor("oc_group", "omt_a", "om_root")
    calls = []

    monkeypatch.setattr(
        instance,
        "_reply_message_sync",
        lambda message_id, msg_type, content: calls.append(
            (message_id, msg_type, content)
        )
        or "om_reply",
    )

    result = instance._send_message_sync(
        "oc_group", "omt_a", "interactive", '{"schema":"2.0"}'
    )

    assert result == "om_reply"
    assert calls == [("om_root", "interactive", '{"schema":"2.0"}')]


def test_feishu_thread_reply_never_falls_back_to_group_root(monkeypatch):
    instance = frontend()
    create_calls = []
    monkeypatch.setattr(
        instance,
        "_create_message_sync",
        lambda *args: create_calls.append(args) or "om_group",
    )

    try:
        instance._send_message_sync(
            "oc_group", "omt_a", "interactive", '{"schema":"2.0"}'
        )
    except FeishuThreadAnchorMissing as exc:
        assert "omt_a" in str(exc)
    else:
        raise AssertionError("missing thread anchor must fail closed")

    assert create_calls == []


def test_feishu_registers_non_routing_chat_events_as_ignored():
    instance = frontend()
    registrations = {}

    class Builder:
        def __getattr__(self, name):
            if name.startswith("register_p2_"):
                return lambda callback: registrations.__setitem__(name, callback)
            raise AttributeError(name)

    instance._register_ignored_events(Builder())

    assert registrations["register_p2_im_chat_updated_v1"].__self__ is instance
    assert registrations["register_p2_im_chat_member_bot_added_v1"].__self__ is instance
    assert registrations["register_p2_im_chat_member_user_added_v1"].__self__ is instance
    assert registrations["register_p2_im_chat_member_user_deleted_v1"].__self__ is instance
    assert registrations["register_p2_im_chat_member_user_withdrawn_v1"].__self__ is instance


def test_feishu_chat_removal_deprovisions_every_topic_route(monkeypatch):
    instance = frontend()
    second = Binding(
        name="topic-b",
        chat_id="oc_group",
        thread_id="omt_b",
        tmux_session="topic-b",
        tmux_window=0,
        tmux_pane=0,
        cwd=Path("/tmp/topic-b"),
        backend="pi",
        channel="feishu",
        bot_token_env="FEISHU",
    )
    instance.bindings.append(second)
    instance.state = SimpleNamespace()
    instance.bindings_file = None
    removed = []

    async def fake_deprovision(_frontend, _state, binding, *, bindings_file):
        removed.append(binding.name)

    monkeypatch.setattr("tmuxbot.provision.deprovision_chat", fake_deprovision)

    asyncio.run(instance._handle_chat_removed("oc_group"))

    assert removed == ["topic-a", "topic-b"]


def test_feishu_thread_v2_edit_keeps_exact_binding_metadata(monkeypatch):
    instance = frontend()
    instance.card_v2_enabled = True
    instance._v2_message_ids = set()
    instance._v2_message_states = {}
    instance._v2_message_footers = {}
    cards = []

    monkeypatch.setattr(
        instance,
        "_send_card_sync",
        lambda chat_id, content, thread_id=None: (
            cards.append(("send", json.loads(content))) or "om_topic"
        ),
    )
    monkeypatch.setattr(
        instance,
        "_patch_card_sync",
        lambda message_id, content: (
            cards.append(("edit", json.loads(content))) or True
        ),
    )

    async def run():
        message = await instance.send_status_html(
            "oc_group", "omt_a", "工作中", display_state="working"
        )
        await instance.edit_html("oc_group", message.message_id, "仍在工作")

    asyncio.run(run())

    assert instance._outbound_routes["om_topic"] == ("oc_group", "omt_a")
    assert cards[1][1]["header"]["title"]["content"].endswith("topic-a")
