from tmuxbot.frontends.telegram import should_leave_unknown_chat


def test_unknown_chat_invited_by_boss_is_kept_for_init():
    assert (
        should_leave_unknown_chat(
            setup_mode=False,
            boss_user_id=42,
            actor_user_id=42,
            bound_count=0,
            removed=False,
        )
        is False
    )


def test_unknown_chat_invited_by_non_boss_is_left():
    assert (
        should_leave_unknown_chat(
            setup_mode=False,
            boss_user_id=42,
            actor_user_id=7,
            bound_count=0,
            removed=False,
        )
        is True
    )


def test_unknown_chat_without_actor_is_left():
    assert (
        should_leave_unknown_chat(
            setup_mode=False,
            boss_user_id=42,
            actor_user_id=None,
            bound_count=0,
            removed=False,
        )
        is True
    )


def test_setup_bound_and_removed_events_do_not_auto_leave():
    assert (
        should_leave_unknown_chat(
            setup_mode=True,
            boss_user_id=42,
            actor_user_id=7,
            bound_count=0,
            removed=False,
        )
        is False
    )
    assert (
        should_leave_unknown_chat(
            setup_mode=False,
            boss_user_id=42,
            actor_user_id=7,
            bound_count=1,
            removed=False,
        )
        is False
    )
    assert (
        should_leave_unknown_chat(
            setup_mode=False,
            boss_user_id=42,
            actor_user_id=7,
            bound_count=0,
            removed=True,
        )
        is False
    )
