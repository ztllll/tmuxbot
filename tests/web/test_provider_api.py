from pathlib import Path

from fastapi.testclient import TestClient

from tmuxbot.control_plane.repository import ControlPlaneRepository
from tmuxbot.control_plane.tmux_inventory import TmuxInventory
from tmuxbot.providers.discovery import ProviderDiscovery
from tmuxbot.web.app import create_app
from tmuxbot.web.settings import WebSettings


PASSWORD = "correct horse battery staple"
SETUP_TOKEN = "0123456789abcdef0123456789abcdef"


def _make_client(
    tmp_path: Path,
) -> tuple[TestClient, ControlPlaneRepository, str]:
    repository = ControlPlaneRepository(tmp_path / "control.sqlite3")
    repository.migrate()
    settings = WebSettings(
        host="127.0.0.1",
        port=8765,
        database_path=repository.path,
        secure_cookie=False,
        setup_token=SETUP_TOKEN,
    )
    client = TestClient(
        create_app(
            settings,
            repository,
            TmuxInventory(),
            [],
            provider_discovery=ProviderDiscovery(),
        ),
        base_url="http://testserver",
        client=("127.0.0.1", 50000),
    )
    bootstrap = client.get("/api/auth/status").json()["csrf_token"]
    setup = client.post(
        "/api/auth/setup",
        json={"password": PASSWORD},
        headers={"X-CSRF-Token": bootstrap, "X-Setup-Token": SETUP_TOKEN},
    )
    assert setup.status_code == 201
    return client, repository, setup.json()["csrf_token"]


def _fake_cli(path: Path, version: str) -> None:
    path.write_text(f"#!/bin/sh\nprintf '{version}\\n'\n", encoding="utf-8")
    path.chmod(0o755)


def test_provider_endpoints_require_authentication_and_csrf(tmp_path, monkeypatch):
    repository = ControlPlaneRepository(tmp_path / "unauth.sqlite3")
    repository.migrate()
    settings = WebSettings("127.0.0.1", 8765, repository.path, False, setup_token=SETUP_TOKEN)
    client = TestClient(create_app(settings, repository, TmuxInventory(), []))

    assert client.get("/api/providers").status_code == 401
    assert client.post("/api/providers/scan").status_code == 401

    authenticated, _, _ = _make_client(tmp_path / "authenticated")
    assert authenticated.post("/api/providers/scan").status_code == 403


def test_scan_and_probe_use_server_provider_id_only(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _fake_cli(bin_dir / "codex", "codex 5.0")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PATH", str(bin_dir))
    client, _, csrf = _make_client(tmp_path)

    scanned = client.post(
        "/api/providers/scan", headers={"X-CSRF-Token": csrf}
    )

    assert scanned.status_code == 200
    [provider] = scanned.json()
    assert provider["binary_name"] == "codex"
    provider_id = provider["id"]
    probed = client.post(
        f"/api/providers/{provider_id}/probe",
        json={"path": "/tmp/browser-controlled-binary"},
        headers={"X-CSRF-Token": csrf},
    )
    assert probed.status_code == 200
    assert probed.json()["version"] == "codex 5.0"
    assert client.post(
        "/api/providers/not-a-server-id/probe",
        json={"path": str(bin_dir / "codex")},
        headers={"X-CSRF-Token": csrf},
    ).status_code == 404


def test_probe_rejects_changed_provider_identity_with_fixed_409(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    executable = bin_dir / "claude"
    _fake_cli(executable, "claude 1")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PATH", str(bin_dir))
    client, _, csrf = _make_client(tmp_path)
    [provider] = client.post(
        "/api/providers/scan", headers={"X-CSRF-Token": csrf}
    ).json()
    replacement = bin_dir / "replacement"
    _fake_cli(replacement, "claude 2")
    replacement.replace(executable)

    response = client.post(
        f"/api/providers/{provider['id']}/probe",
        headers={"X-CSRF-Token": csrf},
    )

    assert response.status_code == 409
    assert response.json() == {"detail": "provider executable changed; rescan required"}
    assert str(executable) not in response.text
