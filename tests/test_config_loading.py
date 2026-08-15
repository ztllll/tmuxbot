from pathlib import Path

import pytest
import yaml

from tmuxbot.config import load_config, save_binding_identity
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


def test_feishu_thread_root_anchor_loads_and_persists_atomically(tmp_path):
    entry = _binding("feishu-topic")
    entry.update(
        {
            "chat_id": "oc_group",
            "thread_id": "omt_topic",
            "thread_root_message_id": "om_root_old",
            "tmux_session": "feishu-topic",
            "cwd": "/tmp/feishu-topic",
            "backend": "pi",
            "bot_token_env": "FEISHU",
            "channel": "feishu",
        }
    )
    bindings_file = tmp_path / "bindings.yaml"
    bindings_file.write_text(
        yaml.safe_dump({"bindings": [entry]}, sort_keys=False),
        encoding="utf-8",
    )

    load_config(tmp_path / "missing.env", bindings_file, tmp_path / "offsets.json")
    topic = S.bindings[0]
    assert topic.thread_root_message_id == "om_root_old"

    topic.thread_root_message_id = "om_root_new"
    save_binding_identity(bindings_file, topic)

    saved = yaml.safe_load(bindings_file.read_text(encoding="utf-8"))["bindings"][0]
    assert saved["thread_root_message_id"] == "om_root_new"
    assert saved["thread_id"] == "omt_topic"
    assert saved["tmux_session"] == "feishu-topic"
    assert bindings_file.stat().st_mode & 0o777 == 0o600


def test_admin_dm_route_defaults_to_dedicated_data_workspace_and_configurable_pi(monkeypatch, tmp_path):
    monkeypatch.setenv("BOSS_USER_ID", "123")
    monkeypatch.setenv("TMUXBOT_ADMIN_ENABLED", "1")
    monkeypatch.setenv("TMUXBOT_ADMIN_TMUX", "tmuxbot-admin")
    monkeypatch.setenv("TMUXBOT_ADMIN_CLI", "pi")
    env_file = tmp_path / "runtime.env"
    env_file.write_text(
        "\n".join(
            (
                "BOSS_USER_ID=123",
                "TMUXBOT_ADMIN_ENABLED=1",
                "TMUXBOT_ADMIN_TMUX=tmuxbot-admin",
                "TMUXBOT_ADMIN_CLI=pi",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    bindings_file = tmp_path / "bindings.yaml"
    bindings_file.write_text(
        yaml.safe_dump({"bindings": [_binding()]}, sort_keys=False),
        encoding="utf-8",
    )
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr("tmuxbot.paths.Path.home", lambda: home)

    load_config(env_file, bindings_file, tmp_path / "offsets.json")

    admin = next(binding for binding in S.bindings if binding.admin)
    assert admin.chat_id == 123
    assert admin.thread_id is None
    assert admin.tmux_target == "tmuxbot-admin:0.0"
    assert admin.cwd == home / ".local/share/tmuxbot/admin"
    assert admin.cwd.is_dir()
    assert admin.cwd.stat().st_mode & 0o777 == 0o700
    assert (admin.cwd / "AGENTS.md").is_file()
    assert (admin.cwd / "ADMIN-RUNBOOK.md").is_file()
    assert (admin.cwd / "tmuxbot-admin-context.json").is_file()
    assert admin.backend == "pi"
    assert admin.bot_token_env == "TG_BOT_TOKEN"


def test_admin_identity_persistence_refreshes_admin_route_metadata(monkeypatch, tmp_path):
    bindings_file = tmp_path / "bindings.yaml"
    stale = {
        "name": "tmuxbot-admin",
        "channel": "telegram",
        "bot_token_env": "TG_BOT_TOKEN",
        "chat_id": 123,
        "thread_id": None,
        "tmux_session": "tmuxbot-admin",
        "tmux_window": 0,
        "tmux_pane": 0,
        "cwd": str(tmp_path / "old-admin"),
        "backend": "pi",
        "admin": True,
        "provider_session_id": "old-session",
    }
    bindings_file.write_text(
        yaml.safe_dump({"bindings": [_binding(), stale]}, sort_keys=False),
        encoding="utf-8",
    )
    new_cwd = tmp_path / "admin"
    binding = Binding(
        name="tmuxbot-admin",
        chat_id=123,
        thread_id=None,
        tmux_session="tmuxbot-admin",
        tmux_window=0,
        tmux_pane=0,
        cwd=new_cwd,
        backend="pi",
        bot_token_env="TG_CODEX_BOT_TOKEN",
        channel="telegram",
        mention_required=False,
        admin=True,
        provider_session_id="new-session",
    )

    save_binding_identity(bindings_file, binding)

    saved = yaml.safe_load(bindings_file.read_text(encoding="utf-8"))["bindings"]
    admin = next(entry for entry in saved if entry.get("admin"))
    assert admin["cwd"] == str(new_cwd)
    assert admin["bot_token_env"] == "TG_CODEX_BOT_TOKEN"
    assert admin["provider_session_id"] == "new-session"


def test_admin_identity_is_appended_and_reused_without_duplicating_route(monkeypatch, tmp_path):
    monkeypatch.setenv("BOSS_USER_ID", "123")
    monkeypatch.setenv("TMUXBOT_ADMIN_ENABLED", "1")
    monkeypatch.setenv("TMUXBOT_ADMIN_CLI", "pi")
    admin_cwd = tmp_path / "admin"
    monkeypatch.setenv("TMUXBOT_ADMIN_CWD", str(admin_cwd))
    bindings_file = tmp_path / "bindings.yaml"
    bindings_file.write_text(
        yaml.safe_dump({"bindings": [_binding()]}, sort_keys=False),
        encoding="utf-8",
    )
    load_config(tmp_path / "missing.env", bindings_file, tmp_path / "offsets.json")
    admin = next(binding for binding in S.bindings if binding.admin)
    admin.provider_session_id = "pi-session"
    admin.last_session_id = "pi-session"
    admin.transcript_path = tmp_path / "session.jsonl"

    save_binding_identity(bindings_file, admin)
    saved = yaml.safe_load(bindings_file.read_text(encoding="utf-8"))["bindings"]
    assert bindings_file.stat().st_mode & 0o777 == 0o600
    assert [entry["name"] for entry in saved].count("tmuxbot-admin") == 1
    assert next(entry for entry in saved if entry.get("admin"))["provider_session_id"] == "pi-session"

    load_config(tmp_path / "missing.env", bindings_file, tmp_path / "offsets.json")
    reloaded = next(binding for binding in S.bindings if binding.admin)
    assert reloaded.provider_session_id == "pi-session"
    assert reloaded.transcript_path == tmp_path / "session.jsonl"
    assert reloaded.cwd == admin_cwd
    assert (admin_cwd / "tmuxbot-admin-context.json").is_file()


def test_persisted_admin_identity_does_not_enable_admin_without_env(tmp_path):
    persisted = _binding("tmuxbot-admin")
    persisted.update(
        {
            "chat_id": 123,
            "tmux_session": "tmuxbot-admin",
            "cwd": "/home/admin",
            "backend": "pi",
            "admin": True,
            "provider_session_id": "pi-session",
        }
    )
    bindings_file = tmp_path / "bindings.yaml"
    bindings_file.write_text(
        yaml.safe_dump({"bindings": [_binding(), persisted]}, sort_keys=False),
        encoding="utf-8",
    )

    load_config(tmp_path / "missing.env", bindings_file, tmp_path / "offsets.json")

    assert all(not binding.admin for binding in S.bindings)
    assert [binding.name for binding in S.bindings] == ["alpha"]


def test_admin_dm_route_can_override_channel_credential_endpoint_and_cwd(monkeypatch, tmp_path):
    monkeypatch.setenv("BOSS_USER_ID", "123")
    monkeypatch.setenv("TMUXBOT_ADMIN_ENABLED", "1")
    monkeypatch.setenv("TMUXBOT_ADMIN_CHANNEL", "feishu")
    monkeypatch.setenv("TMUXBOT_ADMIN_CHAT_ID", "oc_admin")
    monkeypatch.setenv("TMUXBOT_ADMIN_CREDENTIAL", "FEISHU2")
    monkeypatch.setenv("TMUXBOT_ADMIN_CLI", "codex")
    admin_cwd = tmp_path / "admin"
    admin_cwd.mkdir()
    monkeypatch.setenv("TMUXBOT_ADMIN_CWD", str(admin_cwd))
    env_file = tmp_path / "runtime.env"
    env_file.write_text(
        "\n".join(
            (
                "BOSS_USER_ID=123",
                "TMUXBOT_ADMIN_ENABLED=1",
                "TMUXBOT_ADMIN_CHANNEL=feishu",
                "TMUXBOT_ADMIN_CHAT_ID=oc_admin",
                "TMUXBOT_ADMIN_CREDENTIAL=FEISHU2",
                "TMUXBOT_ADMIN_CLI=codex",
                f"TMUXBOT_ADMIN_CWD={admin_cwd}",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    bindings_file = tmp_path / "bindings.yaml"
    bindings_file.write_text(
        yaml.safe_dump({"bindings": [_binding()]}, sort_keys=False),
        encoding="utf-8",
    )

    load_config(env_file, bindings_file, tmp_path / "offsets.json")

    admin = next(binding for binding in S.bindings if binding.admin)
    assert (admin.channel, admin.chat_id, admin.bot_token_env) == (
        "feishu",
        "oc_admin",
        "FEISHU2",
    )
    assert admin.cwd == admin_cwd
    assert admin.backend == "codex"


def test_admin_dm_route_rejects_user_home_as_admin_cwd(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr("tmuxbot.config.Path.home", lambda: home)
    monkeypatch.setenv("BOSS_USER_ID", "123")
    monkeypatch.setenv("TMUXBOT_ADMIN_ENABLED", "1")
    monkeypatch.setenv("TMUXBOT_ADMIN_CLI", "pi")
    monkeypatch.setenv("TMUXBOT_ADMIN_CWD", str(home))
    bindings_file = tmp_path / "bindings.yaml"
    bindings_file.write_text(
        yaml.safe_dump({"bindings": [_binding()]}, sort_keys=False),
        encoding="utf-8",
    )

    with pytest.raises(ConfigValidationError, match="must not be the user home directory"):
        load_config(tmp_path / "missing.env", bindings_file, tmp_path / "offsets.json")


def test_admin_dm_route_creates_configured_private_cwd(monkeypatch, tmp_path):
    monkeypatch.setenv("BOSS_USER_ID", "123")
    monkeypatch.setenv("TMUXBOT_ADMIN_ENABLED", "1")
    monkeypatch.setenv("TMUXBOT_ADMIN_CLI", "pi")
    admin_cwd = tmp_path / "missing" / "admin"
    monkeypatch.setenv("TMUXBOT_ADMIN_CWD", str(admin_cwd))
    bindings_file = tmp_path / "bindings.yaml"
    bindings_file.write_text(
        yaml.safe_dump({"bindings": [_binding()]}, sort_keys=False),
        encoding="utf-8",
    )

    load_config(tmp_path / "missing.env", bindings_file, tmp_path / "offsets.json")

    admin = next(binding for binding in S.bindings if binding.admin)
    assert admin.cwd == admin_cwd
    assert admin_cwd.stat().st_mode & 0o777 == 0o700
    assert (admin_cwd / "AGENTS.md").is_file()


def test_admin_dm_route_rejects_non_private_telegram_endpoint(monkeypatch, tmp_path):
    monkeypatch.setenv("BOSS_USER_ID", "123")
    monkeypatch.setenv("TMUXBOT_ADMIN_ENABLED", "1")
    monkeypatch.setenv("TMUXBOT_ADMIN_CLI", "pi")
    monkeypatch.setenv("TMUXBOT_ADMIN_CHAT_ID", "-100123")
    bindings_file = tmp_path / "bindings.yaml"
    bindings_file.write_text(
        yaml.safe_dump({"bindings": [_binding()]}, sort_keys=False),
        encoding="utf-8",
    )

    with pytest.raises(ConfigValidationError, match="positive private user id"):
        load_config(tmp_path / "missing.env", bindings_file, tmp_path / "offsets.json")


def test_admin_dm_route_requires_explicit_feishu_endpoint(monkeypatch, tmp_path):
    monkeypatch.setenv("BOSS_USER_ID", "123")
    monkeypatch.setenv("TMUXBOT_ADMIN_ENABLED", "1")
    monkeypatch.setenv("TMUXBOT_ADMIN_CHANNEL", "feishu")
    monkeypatch.setenv("TMUXBOT_ADMIN_CLI", "pi")
    env_file = tmp_path / "runtime.env"
    env_file.write_text(
        "BOSS_USER_ID=123\nTMUXBOT_ADMIN_ENABLED=1\n"
        "TMUXBOT_ADMIN_CHANNEL=feishu\nTMUXBOT_ADMIN_CLI=pi\n",
        encoding="utf-8",
    )
    bindings_file = tmp_path / "bindings.yaml"
    bindings_file.write_text(
        yaml.safe_dump({"bindings": [_binding()]}, sort_keys=False),
        encoding="utf-8",
    )

    with pytest.raises(ConfigValidationError, match="TMUXBOT_ADMIN_CHAT_ID"):
        load_config(env_file, bindings_file, tmp_path / "offsets.json")


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
