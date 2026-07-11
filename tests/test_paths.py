import stat
from pathlib import Path

import pytest

from tmuxbot.paths import RuntimePaths


def test_paths_default_to_xdg_under_empty_home(tmp_path: Path):
    home = tmp_path / "home"

    paths = RuntimePaths.discover({}, home=home)

    assert paths.env_file == home / ".config/tmuxbot/.env"
    assert paths.bindings_file == home / ".config/tmuxbot/bindings.yaml"
    assert paths.database_file == home / ".local/share/tmuxbot/control-plane.sqlite3"
    assert paths.offsets_file == home / ".local/state/tmuxbot/offsets.json"
    assert paths.lock_file == home / ".local/state/tmuxbot/tmuxbot.lock"
    assert paths.hook_spool_file == home / ".local/state/tmuxbot/claude-hooks.jsonl"


def test_explicit_tmuxbot_overrides_win_over_xdg(tmp_path: Path):
    paths = RuntimePaths.discover(
        {
            "XDG_CONFIG_HOME": str(tmp_path / "xdg-config"),
            "XDG_DATA_HOME": str(tmp_path / "xdg-data"),
            "XDG_STATE_HOME": str(tmp_path / "xdg-state"),
            "TMUXBOT_ENV": str(tmp_path / "runtime.env"),
            "TMUXBOT_BINDINGS": str(tmp_path / "runtime.yaml"),
            "TMUXBOT_DATA_DIR": str(tmp_path / "product-data"),
            "TMUXBOT_DATABASE": str(tmp_path / "db.sqlite3"),
            "TMUXBOT_OFFSETS": str(tmp_path / "offsets.json"),
            "TMUXBOT_LOCK": str(tmp_path / "bridge.lock"),
            "TMUXBOT_HOOK_SPOOL": str(tmp_path / "hooks.jsonl"),
        },
        home=tmp_path / "home",
    )

    assert paths.env_file == tmp_path / "runtime.env"
    assert paths.bindings_file == tmp_path / "runtime.yaml"
    assert paths.data_dir == tmp_path / "product-data"
    assert paths.database_file == tmp_path / "db.sqlite3"
    assert paths.offsets_file == tmp_path / "offsets.json"
    assert paths.lock_file == tmp_path / "bridge.lock"
    assert paths.hook_spool_file == tmp_path / "hooks.jsonl"


def test_legacy_data_dir_override_keeps_state_files_together(tmp_path: Path):
    data_dir = tmp_path / "legacy-data"

    paths = RuntimePaths.discover(
        {"TMUXBOT_DATA_DIR": str(data_dir)}, home=tmp_path / "home"
    )

    assert paths.database_file == data_dir / "control-plane.sqlite3"
    assert paths.offsets_file == data_dir / "offsets.json"
    assert paths.lock_file == data_dir / "tmuxbot.lock"
    assert paths.hook_spool_file == data_dir / "claude-hooks.jsonl"


def test_explicit_xdg_paths_work_when_home_is_empty(tmp_path: Path):
    paths = RuntimePaths.discover(
        {
            "XDG_CONFIG_HOME": str(tmp_path / "cfg"),
            "XDG_DATA_HOME": str(tmp_path / "share"),
            "XDG_STATE_HOME": str(tmp_path / "state"),
        },
        home=Path(""),
    )

    assert paths.config_dir == tmp_path / "cfg/tmuxbot"
    assert paths.data_dir == tmp_path / "share/tmuxbot"
    assert paths.state_dir == tmp_path / "state/tmuxbot"


def test_installed_package_never_uses_site_packages_as_config_root(tmp_path: Path):
    fake_site_packages = tmp_path / "venv/lib/python3.12/site-packages/tmuxbot"

    paths = RuntimePaths.discover({}, home=tmp_path / "home", legacy_project_dir=fake_site_packages)

    assert fake_site_packages not in paths.env_file.parents
    assert fake_site_packages not in paths.bindings_file.parents


def test_source_checkout_legacy_files_are_used_only_when_present(tmp_path: Path):
    legacy = tmp_path / "checkout"
    legacy.mkdir()
    (legacy / ".env").write_text("BOSS_USER_ID=1\n", encoding="utf-8")

    paths = RuntimePaths.discover({}, home=tmp_path / "home", legacy_project_dir=legacy)

    assert paths.env_file == legacy / ".env"
    assert paths.bindings_file == tmp_path / "home/.config/tmuxbot/bindings.yaml"

    (legacy / "bindings.yaml").write_text("bindings: []\n", encoding="utf-8")
    paths = RuntimePaths.discover({}, home=tmp_path / "home", legacy_project_dir=legacy)
    assert paths.bindings_file == legacy / "bindings.yaml"


def test_private_directories_are_created_with_mode_0700(tmp_path: Path):
    paths = RuntimePaths.discover({}, home=tmp_path / "home")

    paths.ensure_private_directories()

    for directory in (paths.config_dir, paths.data_dir, paths.state_dir):
        assert directory.is_dir()
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700


def test_private_directories_reject_symlinks(tmp_path: Path):
    paths = RuntimePaths.discover({}, home=tmp_path / "home")
    target = tmp_path / "target"
    target.mkdir()
    paths.config_dir.parent.mkdir(parents=True)
    paths.config_dir.symlink_to(target, target_is_directory=True)

    with pytest.raises(OSError, match="symbolic link"):
        paths.ensure_private_directories()
