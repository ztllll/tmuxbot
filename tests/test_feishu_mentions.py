from types import SimpleNamespace

from tmuxbot.frontends.feishu import FeishuFrontend, feishu_message_addresses_bot


def _mention(open_id):
    return SimpleNamespace(id=SimpleNamespace(open_id=open_id))


def _msg(*, mentions=None, parent_id=None, root_id=None):
    return SimpleNamespace(
        mentions=mentions or [],
        parent_id=parent_id,
        root_id=root_id,
    )


def test_feishu_message_addresses_bot_matches_mention_or_reply():
    assert feishu_message_addresses_bot(_msg(mentions=[_mention("ou_bot")]), "ou_bot", set())
    assert feishu_message_addresses_bot(_msg(parent_id="om_bot_reply"), "ou_bot", {"om_bot_reply"})
    assert feishu_message_addresses_bot(_msg(root_id="om_bot_root"), "ou_bot", {"om_bot_root"})


def test_feishu_message_addresses_bot_rejects_unaddressed_group_message():
    assert not feishu_message_addresses_bot(_msg(), "ou_bot", {"om_other"})


def test_feishu_frontend_message_allowed_uses_shared_addressing():
    frontend = object.__new__(FeishuFrontend)
    frontend.group_only_when_mentioned = True
    frontend.app_id = "cli_app"
    frontend.bot_open_id = "ou_bot"
    frontend._outbound_message_ids = {"om_bot_reply"}

    assert frontend._message_allowed_by_addressing("p2p", _msg())
    assert not frontend._message_allowed_by_addressing("group", _msg())
    assert frontend._message_allowed_by_addressing("group", _msg(parent_id="om_bot_reply"))


def test_feishu_panel_control_bypasses_mention_requirement():
    frontend = object.__new__(FeishuFrontend)
    frontend.group_only_when_mentioned = True
    frontend.app_id = "cli_app"
    frontend.bot_open_id = "ou_bot"
    frontend._outbound_message_ids = set()
    frontend.bindings = [
        SimpleNamespace(chat_id="oc_alpha", thread_id=None, mention_required=True)
    ]
    panel = SimpleNamespace(
        chat_id="oc_alpha",
        chat_type="group",
        message_type="text",
        content='{"text":"/panel"}',
        mentions=[],
        parent_id=None,
        root_id=None,
    )
    status = SimpleNamespace(
        chat_id="oc_alpha",
        chat_type="group",
        message_type="text",
        content='{"text":"/status"}',
        mentions=[],
        parent_id=None,
        root_id=None,
    )

    assert frontend._message_allowed_by_addressing("group", panel)
    assert not frontend._message_allowed_by_addressing("group", status)
