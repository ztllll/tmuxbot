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
    web_entrypoint.run_web = lambda *args: calls.append("web")
    monkeypatch.setitem(sys.modules, "tmuxbot.web.__main__", web_entrypoint)

    def reject_bridge(_coroutine):
        raise AssertionError("web runtime must not start bridge polling")

    monkeypatch.setattr("tmuxbot.__main__.asyncio.run", reject_bridge)

    run(["web"])

    assert calls == ["web"]


def test_build_app_only_builds_web_runtime_without_loading_im_configuration(monkeypatch, tmp_path: Path):
    from tmuxbot.web import __main__ as web_main

    settings = types.SimpleNamespace(database_path=tmp_path / "web.sqlite3")
    calls, repositories = [], []
    monkeypatch.setattr(web_main.RuntimePaths, "discover", lambda _env: types.SimpleNamespace(database_file=settings.database_path))
    monkeypatch.setattr(web_main.WebSettings, "from_env", lambda **options: settings)

    class FakeRepository:
        def __init__(self, path):
            repositories.append(self)
            calls.append(("repository", path))
        def migrate(self): calls.append(("migrate",))

    inventory, app = object(), object()
    monkeypatch.setattr(web_main, "ControlPlaneRepository", FakeRepository)
    monkeypatch.setattr(web_main, "TmuxInventory", lambda: inventory)
    monkeypatch.setattr(web_main, "create_app", lambda *args: calls.append(("create_app", args)) or app)

    assert web_main.build_app() == (settings, app)
    assert calls == [
        ("repository", settings.database_path),
        ("migrate",),
        ("create_app", (settings, repositories[0], inventory, [])),
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
