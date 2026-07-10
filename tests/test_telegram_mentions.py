from types import SimpleNamespace

from tmuxbot.frontends.telegram import (
    TelegramFrontend,
    telegram_message_addresses_bot,
    telegram_message_mentions_bot,
)


def _msg(
    text,
    *,
    chat_type="supergroup",
    entities=None,
    caption=None,
    caption_entities=None,
    reply_from_user=None,
):
    return SimpleNamespace(
        text=text,
        caption=caption,
        entities=entities,
        caption_entities=caption_entities,
        chat=SimpleNamespace(type=chat_type, id=-100),
        from_user=SimpleNamespace(id=42),
        reply_to_message=(
            SimpleNamespace(from_user=reply_from_user) if reply_from_user else None
        ),
        is_topic_message=False,
    )


def test_telegram_message_mentions_bot_matches_text_mention():
    assert telegram_message_mentions_bot(_msg("hello @tmuxbot"), "tmuxbot")


def test_telegram_message_mentions_bot_matches_command_suffix_entity():
    message = _msg(
        "/status@tmuxbot",
        entities=[SimpleNamespace(type="bot_command", offset=0, length=15)],
    )

    assert telegram_message_mentions_bot(message, "tmuxbot")


def test_telegram_message_mentions_bot_rejects_unmentioned_group_text():
    assert not telegram_message_mentions_bot(_msg("hello"), "tmuxbot")


def test_telegram_message_addresses_bot_matches_reply_to_bot():
    message = _msg("follow up", reply_from_user=SimpleNamespace(id=100, username="tmuxbot"))

    assert telegram_message_addresses_bot(message, "tmuxbot", 100)


def test_telegram_acl_rejects_unmentioned_group_when_required():
    frontend = object.__new__(TelegramFrontend)
    frontend.state = SimpleNamespace(setup_mode=False, boss_user_id=42)
    frontend.bindings = [SimpleNamespace(chat_id=-100, thread_id=None)]
    frontend.group_only_when_mentioned = True
    frontend._bot_username = "tmuxbot"
    frontend._bot_id = 100

    assert not frontend._acl_ok(_msg("hello"))
    assert frontend._acl_ok(_msg("hello @tmuxbot"))
    assert frontend._acl_ok(
        _msg("follow up", reply_from_user=SimpleNamespace(id=100, username="tmuxbot"))
    )


def test_telegram_acl_allows_private_chat_without_mention_when_required():
    frontend = object.__new__(TelegramFrontend)
    frontend.state = SimpleNamespace(setup_mode=False, boss_user_id=42)
    frontend.bindings = [SimpleNamespace(chat_id=-100, thread_id=None)]
    frontend.group_only_when_mentioned = True
    frontend._bot_username = "tmuxbot"
    frontend._bot_id = 100

    assert frontend._acl_ok(_msg("hello", chat_type="private"))
