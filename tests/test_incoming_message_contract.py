from pathlib import Path
from types import SimpleNamespace

from tmuxbot.channels.feishu import FeishuChannelAdapter
from tmuxbot.channels.telegram import TelegramChannelAdapter
from tmuxbot.core.messages import AttachmentRef


def test_telegram_and_feishu_normalize_equivalent_incoming_messages(tmp_path):
    attachment = AttachmentRef(
        path=str(tmp_path / "input.png"),
        kind="image",
        name="input.png",
        mime_type="image/png",
    )
    telegram_msg = SimpleNamespace(
        message_id=101,
        text="请检查",
        caption=None,
        chat=SimpleNamespace(id=-100, type="supergroup"),
        from_user=SimpleNamespace(id=42),
        is_topic_message=True,
        message_thread_id=7,
        reply_to_message=SimpleNamespace(
            from_user=SimpleNamespace(id=900, username="tmuxbot")
        ),
    )
    feishu_msg = SimpleNamespace(
        message_id="om_101",
        chat_id="oc_100",
        chat_type="group",
        message_type="text",
        content='{"text":"@_user_1 请检查"}',
        mentions=[SimpleNamespace(id=SimpleNamespace(open_id="ou_bot"))],
        parent_id="om_bot_reply",
        root_id=None,
        reply_to_message_id=None,
    )

    telegram = TelegramChannelAdapter(bot_username="tmuxbot", bot_id=900).normalize_incoming(
        telegram_msg, attachments=(attachment,)
    )
    feishu = FeishuChannelAdapter(
        bot_open_id="ou_bot", outbound_message_ids={"om_bot_reply"}
    ).normalize_incoming(
        feishu_msg, sender_id="ou_42", attachments=(attachment,)
    )

    assert telegram.text == feishu.text == "请检查"
    assert telegram.direct_chat == feishu.direct_chat is False
    assert telegram.replied_to_bot == feishu.replied_to_bot is True
    assert telegram.attachments == feishu.attachments == (attachment,)
    assert telegram.source_id == -100
    assert telegram.thread_id == 7
    assert telegram.platform_message_id == 101
    assert feishu.source_id == "oc_100"
    assert feishu.thread_id is None
    assert feishu.platform_message_id == "om_101"


def test_channel_adapters_extract_commands_and_mentions():
    telegram_msg = SimpleNamespace(
        message_id=1,
        text="/status@tmuxbot now",
        caption=None,
        chat=SimpleNamespace(id=42, type="private"),
        from_user=SimpleNamespace(id=42),
        is_topic_message=False,
        message_thread_id=None,
        reply_to_message=None,
    )
    feishu_msg = SimpleNamespace(
        message_id="om_1",
        chat_id="oc_1",
        chat_type="p2p",
        message_type="text",
        content='{"text":"/status now"}',
        mentions=[],
        parent_id=None,
        root_id=None,
        reply_to_message_id=None,
    )

    telegram = TelegramChannelAdapter(bot_username="tmuxbot", bot_id=9).normalize_incoming(
        telegram_msg
    )
    feishu = FeishuChannelAdapter(bot_open_id="ou_bot").normalize_incoming(
        feishu_msg, sender_id="ou_42"
    )

    assert telegram.command == "/status"
    assert telegram.mentioned
    assert telegram.direct_chat
    assert feishu.command == "/status"
    assert feishu.direct_chat


def test_attachment_ref_factory_is_provider_neutral(tmp_path):
    from tmuxbot.attachments import attachment_ref

    path = Path(tmp_path) / "report.pdf"
    path.write_bytes(b"pdf")

    ref = attachment_ref(path, kind="file", mime_type="application/pdf")

    assert ref == AttachmentRef(
        path=str(path),
        kind="file",
        name="report.pdf",
        mime_type="application/pdf",
    )
