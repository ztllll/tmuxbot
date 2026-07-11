from pathlib import Path

from fastapi.testclient import TestClient

from tmuxbot.control_plane.repository import ControlPlaneRepository
from tmuxbot.web import app as app_module
from tmuxbot.web.app import create_app
from tmuxbot.web.settings import WebSettings


class EmptyInventory:
    def list_panes(self):
        return []


def client_for(tmp_path: Path, static_dir: Path, monkeypatch) -> TestClient:
    monkeypatch.setattr(app_module, "STATIC_DIR", static_dir)
    settings = WebSettings(
        host="127.0.0.1",
        port=8765,
        database_path=tmp_path / "control.sqlite3",
        secure_cookie=False,
        setup_token="0123456789abcdef0123456789abcdef",
    )
    repository = ControlPlaneRepository(settings.database_path)
    repository.migrate()
    return TestClient(create_app(settings, repository, EmptyInventory(), []))


def test_fastapi_serves_built_assets_and_spa_fallback(tmp_path, monkeypatch):
    static_dir = tmp_path / "static"
    assets_dir = static_dir / "assets"
    assets_dir.mkdir(parents=True)
    (static_dir / "index.html").write_text(
        '<!doctype html><div id="root">command-center</div>', encoding="utf-8"
    )
    (assets_dir / "app.js").write_text("window.tmuxbot = true", encoding="utf-8")
    client = client_for(tmp_path, static_dir, monkeypatch)

    assert client.get("/").text.endswith('<div id="root">command-center</div>')
    assert client.get("/setup").text.endswith('<div id="root">command-center</div>')
    assert client.get("/providers").text.endswith('<div id="root">command-center</div>')
    asset = client.get("/assets/app.js")
    assert asset.status_code == 200
    assert asset.text == "window.tmuxbot = true"
    assert client.get("/api/health").json() == {"status": "ok"}


def test_missing_frontend_build_keeps_api_healthy_and_explains_recovery(
    tmp_path, monkeypatch
):
    client = client_for(tmp_path, tmp_path / "missing-static", monkeypatch)

    response = client.get("/")

    assert response.status_code == 503
    assert "WebUI 尚未构建" in response.text
    assert "npm run build" in response.text
    assert client.get("/api/health").json() == {"status": "ok"}


def test_static_asset_mount_rejects_directory_traversal(tmp_path, monkeypatch):
    static_dir = tmp_path / "static"
    (static_dir / "assets").mkdir(parents=True)
    (static_dir / "index.html").write_text("index", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("must-not-leak", encoding="utf-8")
    client = client_for(tmp_path, static_dir, monkeypatch)

    response = client.get("/assets/%2e%2e/secret.txt")

    assert response.status_code == 404
    assert "must-not-leak" not in response.text
