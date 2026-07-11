from pathlib import Path

import pytest
import yaml

from tmuxbot.config import load_config
from tmuxbot.state import Binding, S
from tmuxbot.validation import ConfigValidationError


def _binding(name: str = "alpha") -> dict[str, object]:
    return {
        "name": name,
        "chat_id": 1,
        "thread_id": None,
        "tmux_session": f"{name}-claude",
        "tmux_window": 0,
        "tmux_pane": 0,
        "cwd": f"/tmp/{name}",
        "backend": "claude_code",
        "bot_token_env": "TG_BOT_TOKEN",
        "channel": "telegram",
    }


@pytest.fixture(autouse=True)
def restore_state():
    old = (S.boss_user_id, S.setup_mode, S.bindings, S.offsets)
    yield
    S.boss_user_id, S.setup_mode, S.bindings, S.offsets = old


def test_web_config_allows_missing_env_and_bindings(tmp_path: Path):
    load_config(
        tmp_path / "missing.env",
        tmp_path / "missing.yaml",
        tmp_path / "missing-offsets.json",
        allow_missing_bindings=True,
        allow_empty_bindings=True,
    )

    assert S.bindings == []
    assert S.offsets == {}


def test_web_config_allows_bindings_empty_list(tmp_path: Path):
    bindings_file = tmp_path / "bindings.yaml"
    bindings_file.write_text("bindings: []\n", encoding="utf-8")

    load_config(
        tmp_path / "missing.env",
        bindings_file,
        tmp_path / "offsets.json",
        allow_empty_bindings=True,
    )

    assert S.bindings == []


def test_bridge_config_still_rejects_missing_bindings(tmp_path: Path):
    with pytest.raises(ConfigValidationError, match="bindings file does not exist"):
        load_config(
            tmp_path / "missing.env",
            tmp_path / "missing.yaml",
            tmp_path / "offsets.json",
        )


@pytest.mark.parametrize("contents", ["bindings: [", "bindings: nope\n", "[]\n"])
def test_invalid_yaml_never_becomes_unconfigured(tmp_path: Path, contents: str):
    bindings_file = tmp_path / "bindings.yaml"
    bindings_file.write_text(contents, encoding="utf-8")

    with pytest.raises(ConfigValidationError):
        load_config(
            tmp_path / "missing.env",
            bindings_file,
            tmp_path / "offsets.json",
            allow_missing_bindings=True,
            allow_empty_bindings=True,
        )


def test_failed_reload_does_not_partially_mutate_global_state(tmp_path: Path):
    original = Binding(
        name="original",
        chat_id=1,
        thread_id=None,
        tmux_session="original-claude",
        tmux_window=0,
        tmux_pane=0,
        cwd=Path("/tmp/original"),
    )
    S.boss_user_id = 99
    S.setup_mode = False
    S.bindings = [original]
    S.offsets = {"original": 42}
    env_file = tmp_path / "runtime.env"
    env_file.write_text("BOSS_USER_ID=123\n", encoding="utf-8")
    bindings_file = tmp_path / "bindings.yaml"
    bindings_file.write_text(yaml.safe_dump({"bindings": [_binding(), _binding()]}), encoding="utf-8")

    with pytest.raises(ConfigValidationError):
        load_config(env_file, bindings_file, tmp_path / "offsets.json")

    assert (S.boss_user_id, S.setup_mode, S.bindings, S.offsets) == (
        99,
        False,
        [original],
        {"original": 42},
    )
