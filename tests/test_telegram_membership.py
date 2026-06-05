from tmuxbot.frontends.telegram import should_grace_unknown_chat


def test_unknown_chat_gets_init_grace_window():
    assert (
        should_grace_unknown_chat(
            setup_mode=False,
            bound_count=0,
            removed=False,
        )
        is True
    )


def test_setup_bound_and_removed_events_do_not_get_grace_window():
    assert (
        should_grace_unknown_chat(
            setup_mode=True,
            bound_count=0,
            removed=False,
        )
        is False
    )
    assert (
        should_grace_unknown_chat(
            setup_mode=False,
            bound_count=1,
            removed=False,
        )
        is False
    )
    assert (
        should_grace_unknown_chat(
            setup_mode=False,
            bound_count=0,
            removed=True,
        )
        is False
    )


def test_unknown_chat_grace_policy_does_not_depend_on_inviter_identity():
    assert (
        should_grace_unknown_chat(
            setup_mode=False,
            bound_count=0,
            removed=False,
        )
        is True
    )
    assert (
        should_grace_unknown_chat(
            setup_mode=False,
            bound_count=0,
            removed=False,
        )
        is True
    )
