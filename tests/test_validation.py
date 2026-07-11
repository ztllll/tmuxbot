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


def test_rejects_duplicate_source_session_and_cwd():
    assert_invalid(
        [
            binding(),
            binding(name="beta", tmux_session="alpha-claude"),
        ],
        "duplicate source",
        "duplicate tmux_session",
        "duplicate tmux target",
        "duplicate cwd",
    )


def test_rejects_telegram_backend_token_mismatch():
    assert_invalid(
        [binding(name="codex-on-claude-token", backend="codex")],
        "does not match",
    )


def test_rejects_bad_channel_and_backend():
    assert_invalid(
        [binding(channel="discord", backend="unknown")],
        "unsupported channel",
        "unsupported backend",
    )


def test_rejects_feishu_thread_and_mixed_backend_per_env():
    assert_invalid(
        [
            binding(
                name="fs-a",
                channel="feishu",
                chat_id="oc_a",
                thread_id=1,
                tmux_session="fs-a",
                cwd=Path("/tmp/fs-a"),
                bot_token_env="FEISHU",
            ),
            binding(
                name="fs-b",
                channel="feishu",
                chat_id="oc_b",
                tmux_session="fs-b",
                cwd=Path("/tmp/fs-b"),
                backend="codex",
                bot_token_env="FEISHU",
            ),
        ],
        "feishu thread_id must be null",
        "mixes backend",
    )
