import asyncio
from pathlib import Path
from types import SimpleNamespace

from tmuxbot.channels.feishu import feishu_topic_reference, parse_feishu_topic_reference
from tmuxbot.frontends.feishu import FeishuFrontend
from tmuxbot.state import Binding


SECRET = "test-app-secret"


def _admin() -> Binding:
    return Binding(
        name="tmuxbot-admin", chat_id="oc_admin_dm", thread_id=None,
        tmux_session="pi-tmuxbot-admin", tmux_window=0, tmux_pane=0,
        cwd=Path("/tmp/admin"), backend="pi", channel="feishu",
        bot_token_env="FEISHU", admin=True,
    )


def _frontend() -> FeishuFrontend:
    frontend = object.__new__(FeishuFrontend)
    frontend.bindings = [_admin()]
    frontend.backends = {"pi": object()}
    frontend.state = SimpleNamespace()
    frontend.app_secret = SECRET
    return frontend


def _topic_message():
    return SimpleNamespace(
        chat_id="oc_project_group", thread_id="omt_project_topic",
        root_id="om_topic_root", message_id="om_message",
    )


def test_signed_topic_reference_round_trips_only_for_issuing_app():
    reference = feishu_topic_reference(_topic_message(), SECRET)

    assert reference is not None
    assert parse_feishu_topic_reference(reference, SECRET) == (
        "oc_project_group", "omt_project_topic", "om_topic_root",
    )
    assert parse_feishu_topic_reference(reference, "different-app-secret") is None
    assert parse_feishu_topic_reference(reference[:-1] + "0", SECRET) is None


def test_topic_reference_request_requires_exact_thread_mention_and_command():
    frontend = _frontend()
    valid = SimpleNamespace(text="/project-context", thread_id="omt_topic", mentioned=True)

    assert frontend._is_topic_reference_request(valid, "group")
    assert not frontend._is_topic_reference_request(
        SimpleNamespace(text="/project-context", thread_id="omt_topic", mentioned=False), "group"
    )
    assert not frontend._is_topic_reference_request(
        SimpleNamespace(text="/project-context", thread_id=None, mentioned=True), "group"
    )
    assert not frontend._is_topic_reference_request(valid, "p2p")


def test_topic_reference_response_stays_in_topic(monkeypatch):
    frontend = _frontend()
    sent = []

    async def send_html(*args):
        sent.append(args)

    frontend.send_html = send_html
    incoming = SimpleNamespace(source_id="oc_project_group", thread_id="omt_project_topic")

    asyncio.run(frontend._send_topic_reference(_topic_message(), incoming))

    assert sent[0][:2] == ("oc_project_group", "omt_project_topic")
    reference = sent[0][2].split("<code>", 1)[1].split("</code>", 1)[0]
    assert parse_feishu_topic_reference(reference, SECRET) == (
        "oc_project_group", "omt_project_topic", "om_topic_root",
    )


def test_admin_dm_prompt_uses_verified_topic_reference():
    frontend = _frontend()
    reference = feishu_topic_reference(_topic_message(), SECRET)
    prompt = frontend._admin_topic_prompt("创建项目，cwd=/data/demo，adapter=pi", parse_feishu_topic_reference(reference, SECRET))

    assert "chat_id: oc_project_group" in prompt
    assert "thread_id: omt_project_topic" in prompt
    assert "thread_root_message_id: om_topic_root" in prompt
