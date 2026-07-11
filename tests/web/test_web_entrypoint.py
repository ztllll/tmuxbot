import os
import sys
import types
from pathlib import Path

import pytest

from tmuxbot.__main__ import build_parser
from tmuxbot.__main__ import run


def test_cli_exposes_web_subcommand_without_changing_default_runtime():
    parser = build_parser()

    assert parser.parse_args([]).command == "bridge"
    assert parser.parse_args(["web"]).command == "web"


def test_web_subcommand_starts_only_the_web_runtime(monkeypatch):
    calls = []
    web_entrypoint = types.ModuleType("tmuxbot.web.__main__")
    web_entrypoint.run_web = lambda: calls.append("web")
    monkeypatch.setitem(sys.modules, "tmuxbot.web.__main__", web_entrypoint)

    def reject_bridge(_coroutine):
        raise AssertionError("web runtime must not start bridge polling")

    monkeypatch.setattr("tmuxbot.__main__.asyncio.run", reject_bridge)

    run(["web"])

    assert calls == ["web"]


def test_build_app_loads_config_migrates_database_and_wires_dependencies(
    monkeypatch, tmp_path: Path
):
    from tmuxbot.web import __main__ as web_main

    data_dir = tmp_path / "data"
    env_file = tmp_path / "runtime.env"
    bindings_file = tmp_path / "runtime-bindings.yaml"
    monkeypatch.setenv("TMUXBOT_DATA_DIR", str(data_dir))
    monkeypatch.setenv("TMUXBOT_ENV", str(env_file))
    monkeypatch.setenv("TMUXBOT_BINDINGS", str(bindings_file))

    settings = types.SimpleNamespace(database_path=tmp_path / "web.sqlite3")
    calls = []

    def load_runtime_env(path, *, override):
        calls.append(("load_dotenv", path, override))

    def load_runtime_config(*paths, **options):
        calls.append(("load_config", paths, options))

    def settings_from_loaded_env(*, database_path):
        assert [call[0] for call in calls] == ["load_dotenv", "load_config"]
        calls.append(("settings", database_path))
        return settings

    monkeypatch.setattr(web_main.WebSettings, "from_env", settings_from_loaded_env)
    monkeypatch.setattr(web_main, "load_dotenv", load_runtime_env)
    monkeypatch.setattr(
        web_main,
        "load_config",
        load_runtime_config,
    )
    repositories = []

    class FakeRepository:
        def __init__(self, path):
            repositories.append(self)
            calls.append(("repository", path))

        def migrate(self):
            calls.append(("migrate",))

    inventory = object()
    app = object()
    monkeypatch.setattr(web_main, "ControlPlaneRepository", FakeRepository)
    monkeypatch.setattr(web_main, "TmuxInventory", lambda: inventory)
    monkeypatch.setattr(
        web_main,
        "create_app",
        lambda *args: calls.append(("create_app", args)) or app,
    )
    monkeypatch.setattr(web_main.S, "bindings", ["binding"])

    assert web_main.build_app() == (settings, app)
    assert calls == [
        ("load_dotenv", env_file, False),
        (
            "load_config",
            (env_file, bindings_file, data_dir / "offsets.json"),
            {"allow_missing_bindings": True, "allow_empty_bindings": True},
        ),
        ("settings", data_dir / "control-plane.sqlite3"),
        ("repository", settings.database_path),
        ("migrate",),
        ("create_app", (settings, repositories[0], inventory, ["binding"])),
    ]


def test_build_app_uses_data_and_bindings_paths_from_custom_env_file(
    monkeypatch, tmp_path: Path
):
    from tmuxbot.web import __main__ as web_main

    monkeypatch.setattr(os, "environ", os.environ.copy())
    data_dir = tmp_path / "custom-data"
    bindings_file = tmp_path / "custom-bindings.yaml"
    env_file = tmp_path / "custom.env"
    env_file.write_text(
        f"TMUXBOT_DATA_DIR={data_dir}\nTMUXBOT_BINDINGS={bindings_file}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("TMUXBOT_ENV", str(env_file))
    monkeypatch.delenv("TMUXBOT_DATA_DIR", raising=False)
    monkeypatch.delenv("TMUXBOT_BINDINGS", raising=False)
    loaded_paths = []
    monkeypatch.setattr(
        web_main, "load_config", lambda *paths, **options: loaded_paths.append(paths)
    )
    settings = types.SimpleNamespace(database_path=tmp_path / "web.sqlite3")
    monkeypatch.setattr(
        web_main.WebSettings, "from_env", lambda **options: settings
    )
    monkeypatch.setattr(web_main.ControlPlaneRepository, "migrate", lambda self: None)
    monkeypatch.setattr(web_main, "create_app", lambda *args: object())

    web_main.build_app()

    assert loaded_paths == [(env_file, bindings_file, data_dir / "offsets.json")]


def test_build_app_preserves_external_paths_over_custom_env_file(
    monkeypatch, tmp_path: Path
):
    from tmuxbot.web import __main__ as web_main

    monkeypatch.setattr(os, "environ", os.environ.copy())
    env_data_dir = tmp_path / "env-data"
    env_bindings_file = tmp_path / "env-bindings.yaml"
    external_data_dir = tmp_path / "external-data"
    external_bindings_file = tmp_path / "external-bindings.yaml"
    env_file = tmp_path / "custom.env"
    env_file.write_text(
        f"TMUXBOT_DATA_DIR={env_data_dir}\nTMUXBOT_BINDINGS={env_bindings_file}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("TMUXBOT_ENV", str(env_file))
    monkeypatch.setenv("TMUXBOT_DATA_DIR", str(external_data_dir))
    monkeypatch.setenv("TMUXBOT_BINDINGS", str(external_bindings_file))
    loaded_paths = []
    monkeypatch.setattr(
        web_main, "load_config", lambda *paths, **options: loaded_paths.append(paths)
    )
    settings = types.SimpleNamespace(database_path=tmp_path / "web.sqlite3")
    monkeypatch.setattr(
        web_main.WebSettings, "from_env", lambda **options: settings
    )
    monkeypatch.setattr(web_main.ControlPlaneRepository, "migrate", lambda self: None)
    monkeypatch.setattr(web_main, "create_app", lambda *args: object())

    web_main.build_app()

    assert loaded_paths == [
        (env_file, external_bindings_file, external_data_dir / "offsets.json")
    ]


def test_build_app_fails_fast_for_short_setup_token(monkeypatch, tmp_path: Path):
    from tmuxbot.web import __main__ as web_main

    monkeypatch.setattr(os, "environ", os.environ.copy())
    env_file = tmp_path / "custom.env"
    env_file.write_text("TMUXBOT_WEB_SETUP_TOKEN=too-short\n", encoding="utf-8")
    monkeypatch.setenv("TMUXBOT_ENV", str(env_file))
    monkeypatch.delenv("TMUXBOT_WEB_SETUP_TOKEN", raising=False)
    monkeypatch.setattr(web_main, "load_config", lambda *paths, **options: None)

    with pytest.raises(
        ValueError,
        match="TMUXBOT_WEB_SETUP_TOKEN must be an ASCII string at least 24 characters long",
    ):
        web_main.build_app()


def test_run_web_uses_configured_listener_without_trusting_proxy_headers(monkeypatch):
    from tmuxbot.web import __main__ as web_main

    settings = types.SimpleNamespace(host="127.0.0.1", port=8765)
    app = object()
    calls = []
    monkeypatch.setattr(web_main, "build_app", lambda: (settings, app))
    monkeypatch.setattr(
        web_main.uvicorn,
        "run",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    web_main.run_web()

    assert calls == [
        ((app,), {"host": "127.0.0.1", "port": 8765, "proxy_headers": False})
    ]


def test_systemd_unit_is_a_secret_free_user_service():
    unit = Path("deploy/systemd/tmuxbot-web.service").read_text()
    exec_start = next(
        line for line in unit.splitlines() if line.startswith("ExecStart=")
    )

    assert "WantedBy=default.target" in unit
    assert "multi-user.target" not in unit
    assert "WorkingDirectory=" not in unit
    assert "EnvironmentFile=-%h/.config/tmuxbot/.env" in unit
    assert exec_start == "ExecStart=%h/.local/bin/tmuxbot web"
    assert "password" not in exec_start.lower()
    assert "secret" not in exec_start.lower()
