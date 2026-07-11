import sys
import types
from pathlib import Path

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

    def load_runtime_config(*paths):
        calls.append(("load_config", paths))

    def settings_from_loaded_env():
        assert calls and calls[0][0] == "load_config"
        calls.append(("settings",))
        return settings

    monkeypatch.setattr(web_main.WebSettings, "from_env", settings_from_loaded_env)
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
        ("load_config", (env_file, bindings_file, data_dir / "offsets.json")),
        ("settings",),
        ("repository", settings.database_path),
        ("migrate",),
        ("create_app", (settings, repositories[0], inventory, ["binding"])),
    ]


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
