from pathlib import Path

import pytest

from tmuxbot.state import Binding
from tmuxbot.validation import ConfigValidationError, validate_bindings


def binding(**overrides):
    data = {
        "name": "alpha",
        "chat_id": 123,
        "thread_id": None,
        "tmux_session": "alpha-claude",
        "tmux_window": 0,
        "tmux_pane": 0,
        "cwd": Path("/tmp/tmuxbot-alpha"),
        "backend": "claude_code",
        "bot_token_env": "TG_BOT_TOKEN",
        "channel": "telegram",
    }
    data.update(overrides)
    return Binding(**data)


def assert_invalid(bindings, *needles):
    with pytest.raises(ConfigValidationError) as exc:
        validate_bindings(bindings)
    message = str(exc.value)
    for needle in needles:
        assert needle in message


def test_accepts_valid_telegram_and_feishu_bindings():
    validate_bindings(
        [
            binding(),
            binding(
                name="beta",
                channel="feishu",
                chat_id="oc_123",
                thread_id=None,
                tmux_session="beta-claude",
                cwd=Path("/tmp/tmuxbot-beta"),
                bot_token_env="FEISHU",
            ),
        ]
    )


def test_allows_no_bindings_when_explicitly_requested():
    validate_bindings([], require_nonempty=False)


def test_rejects_duplicate_source_target_and_cwd():
    assert_invalid(
        [
            binding(),
            binding(name="beta", tmux_session="alpha-claude"),
        ],
        "duplicate source",
        "duplicate tmux target",
        "duplicate cwd",
    )


def test_allows_multiple_routes_to_distinct_panes_in_one_tmux_session():
    validate_bindings(
        [
            binding(),
            binding(
                name="beta",
                chat_id=123,
                thread_id=42,
                tmux_session="alpha-claude",
                tmux_pane=1,
                cwd=Path("/tmp/tmuxbot-beta"),
            ),
        ]
    )


def test_accepts_multiple_backends_on_one_telegram_credential():
    validate_bindings(
        [
            binding(),
            binding(
                name="beta",
                chat_id=123,
                thread_id=42,
                tmux_session="beta-codex",
                cwd=Path("/tmp/tmuxbot-beta"),
                backend="codex",
            ),
            binding(
                name="gamma",
                chat_id=123,
                thread_id=43,
                tmux_session="gamma-omp",
                cwd=Path("/tmp/tmuxbot-gamma"),
                backend="omp",
            ),
        ]
    )


def test_rejects_bad_channel_and_backend():
    assert_invalid(
        [binding(channel="discord", backend="unknown")],
        "unsupported channel",
        "unsupported backend",
    )


def test_rejects_historical_pi_backend_identity():
    assert_invalid([binding(backend="pi")], "unsupported backend", "'pi'")


def test_accepts_feishu_string_threads_and_mixed_backends_per_credential():
    validate_bindings(
        [
            binding(
                name="fs-a",
                channel="feishu",
                chat_id="oc_a",
                thread_id="omt_thread_a",
                tmux_session="fs-a",
                cwd=Path("/tmp/fs-a"),
                bot_token_env="FEISHU",
            ),
            binding(
                name="fs-b",
                channel="feishu",
                chat_id="oc_a",
                thread_id="omt_thread_b",
                tmux_session="fs-b",
                cwd=Path("/tmp/fs-b"),
                backend="omp",
                bot_token_env="FEISHU",
            ),
        ]
    )


def test_rejects_telegram_admin_route_bound_to_group_id():
    assert_invalid(
        [binding(chat_id=-100, admin=True)],
        "telegram admin route chat_id must be a positive private user id",
    )


def test_rejects_multiple_admin_routes():
    assert_invalid(
        [
            binding(admin=True),
            binding(
                name="admin-two",
                chat_id=2,
                tmux_session="admin-two",
                cwd=Path("/tmp/admin-two"),
                admin=True,
            ),
        ],
        "only one admin route is allowed",
    )


def test_rejects_invalid_feishu_thread_root_message_id():
    assert_invalid(
        [
            binding(
                channel="feishu",
                chat_id="oc_a",
                thread_id="omt_a",
                thread_root_message_id="not-a-message",
                tmux_session="fs-a",
                cwd=Path("/tmp/fs-a"),
                bot_token_env="FEISHU",
            )
        ],
        "thread_root_message_id must start with 'om_'",
    )


def test_rejects_invalid_thread_types_for_each_channel():
    assert_invalid(
        [binding(thread_id="telegram-string")],
        "telegram thread_id must be an integer or null",
    )
    assert_invalid(
        [
            binding(
                channel="feishu",
                chat_id="oc_a",
                thread_id=123,
                tmux_session="fs-a",
                cwd=Path("/tmp/fs-a"),
                bot_token_env="FEISHU",
            )
        ],
        "feishu thread_id must be a string or null",
    )
