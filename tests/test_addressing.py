from tmuxbot.addressing import message_is_addressed_to_bot


def test_group_message_is_addressed_by_mention_or_reply():
    assert message_is_addressed_to_bot(
        require_addressing=True,
        direct_chat=False,
        mentioned=True,
        replied_to_bot=False,
    )
    assert message_is_addressed_to_bot(
        require_addressing=True,
        direct_chat=False,
        mentioned=False,
        replied_to_bot=True,
    )


def test_group_message_requires_addressing_when_enabled():
    assert not message_is_addressed_to_bot(
        require_addressing=True,
        direct_chat=False,
        mentioned=False,
        replied_to_bot=False,
    )


def test_direct_chat_and_disabled_filter_are_allowed():
    assert message_is_addressed_to_bot(
        require_addressing=True,
        direct_chat=True,
        mentioned=False,
        replied_to_bot=False,
    )
    assert message_is_addressed_to_bot(
        require_addressing=False,
        direct_chat=False,
        mentioned=False,
        replied_to_bot=False,
    )
