import asyncio
from pathlib import Path
from types import SimpleNamespace

from tmuxbot.frontends.feishu import FeishuFrontend
from tmuxbot.state import Binding


def _admin() -> Binding:
    return Binding(
        name="tmuxbot-admin",
        chat_id="oc_admin_dm",
        thread_id=None,
        tmux_session="pi-tmuxbot-admin",
        tmux_window=0,
        tmux_pane=0,
        cwd=Path("/tmp/admin"),
        backend="pi",
        channel="feishu",
        bot_token_env="FEISHU",
        admin=True,
    )


def _frontend() -> FeishuFrontend:
    frontend = object.__new__(FeishuFrontend)
    frontend.bindings = [_admin()]
    frontend.backends = {"pi": object()}
    frontend.state = SimpleNamespace(admin_delivery_contexts={})
    return frontend


def test_feishu_bot_open_id_is_resolved_from_bot_info_api(monkeypatch):
    frontend = _frontend()
    frontend.app_id = "cli_app"
    frontend.app_secret = "secret"
    frontend.bot_token_env = "FEISHU_CODEX"
    frontend.bot_open_id = ""
    responses = iter((
        {"code": 0, "tenant_access_token": "token"},
        {"code": 0, "bot": {"open_id": "ou_real_bot"}},
    ))

    class Response:
        def __enter__(self): return self
        def __exit__(self, *_args): return None

    monkeypatch.setattr("tmuxbot.frontends.feishu.urllib.request.urlopen", lambda *_args, **_kwargs: Response())
    monkeypatch.setattr("tmuxbot.frontends.feishu.json.load", lambda _response: next(responses))

    asyncio.run(frontend._ensure_bot_open_id())

    assert frontend.bot_open_id == "ou_real_bot"


def test_topic_admin_request_requires_exact_thread_mention_and_creation_intent():
    frontend = _frontend()
    valid = SimpleNamespace(
        text="@机器人 请创建项目，cwd=/data/demo，adapter=pi",
        thread_id="omt_topic",
        mentioned=True,
    )

    assert frontend._is_topic_admin_request(valid, "group")
    assert not frontend._is_topic_admin_request(
        SimpleNamespace(text="请创建项目", thread_id="omt_topic", mentioned=False),
        "group",
    )
    assert not frontend._is_topic_admin_request(
        SimpleNamespace(text="@机器人 请创建项目", thread_id=None, mentioned=True),
        "group",
    )
    assert not frontend._is_topic_admin_request(valid, "p2p")


def test_topic_admin_request_forwards_verified_endpoint_to_admin_pane(monkeypatch):
    frontend = _frontend()
    calls = []

    async def dispatch(*args):
        calls.append(args)

    monkeypatch.setattr("tmuxbot.dispatch.dispatch_incoming_text", dispatch)
    incoming = SimpleNamespace(
        source_id="oc_project_group",
        thread_id="omt_project_topic",
        text="@机器人 创建项目，cwd=/data/project/demo，adapter=pi",
    )
    message = SimpleNamespace(root_id="om_topic_root", message_id="om_message")

    asyncio.run(frontend._dispatch_topic_admin_request(incoming, message))

    assert frontend.state.admin_delivery_contexts == {
        "tmuxbot-admin": {
            "chat_id": "oc_project_group",
            "thread_id": "omt_project_topic",
            "thread_root_message_id": "om_topic_root",
        }
    }
    assert frontend._thread_reply_anchors[("oc_project_group", "omt_project_topic")] == "om_topic_root"
    assert calls[0][4:6] == ("oc_project_group", "omt_project_topic")
    prompt = calls[0][6]
    assert "chat_id: oc_project_group" in prompt
    assert "thread_id: omt_project_topic" in prompt
    assert "thread_root_message_id: om_topic_root" in prompt
