import os
import time
from pathlib import Path

from fastapi.testclient import TestClient

from tmuxbot.control_plane.models import ManagedSession, ProjectRecord, ProviderProfile
from tmuxbot.control_plane.repository import ControlPlaneRepository
from tmuxbot.control_plane.tmux_inventory import TmuxInventory
from tmuxbot.paths import RuntimePaths
from tmuxbot.web.app import create_app
from tmuxbot.web.settings import WebSettings


def test_channel_configuration_writes_private_legacy_snapshot(tmp_path: Path) -> None:
    paths = RuntimePaths.discover({}, home=tmp_path)
    paths.ensure_private_directories()
    repository = ControlPlaneRepository(paths.database_file)
    repository.migrate()
    now = int(time.time())
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    project_info = project_dir.stat()
    binary = tmp_path / "codex"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o755)
    binary_info = binary.stat()
    provider = ProviderProfile("provider-1", "codex", str(binary), "codex", binary_info.st_dev, binary_info.st_ino, binary_info.st_mtime_ns, now)
    project = ProjectRecord("project-1", "演示", str(project_dir), project_info.st_dev, project_info.st_ino, project_info.st_mtime_ns, now)
    repository.upsert_provider_profile(provider)
    repository.create_project(project)
    repository.create_managed_session(ManagedSession("session-1", project.id, provider.id, "Codex", "codex-demo", 0, 0, "running", now))
    settings = WebSettings("127.0.0.1", 8765, paths.database_file, False, setup_token="0123456789abcdef0123456789abcdef")
    client = TestClient(create_app(settings, repository, TmuxInventory(), [], runtime_paths=paths), client=("127.0.0.1", 50000))
    bootstrap = client.get("/api/auth/status").json()["csrf_token"]
    setup = client.post("/api/auth/setup", json={"password": "correct horse battery staple"}, headers={"X-CSRF-Token": bootstrap, "X-Setup-Token": settings.setup_token})
    csrf = setup.json()["csrf_token"]

    response = client.post("/api/channels/configure", json={
        "channel": "telegram", "managed_session_id": "session-1",
        "remote_chat_id": "123", "credential_id": "123456:secret-token",
        "boss_id": "456", "mention_required": False,
    }, headers={"X-CSRF-Token": csrf})

    assert response.status_code == 201
    assert paths.env_file.stat().st_mode & 0o777 == 0o600
    assert paths.bindings_file.stat().st_mode & 0o777 == 0o600
    assert "TG_CODEX_BOT_TOKEN=123456:secret-token" in paths.env_file.read_text()
    listed = client.get("/api/channels")
    assert listed.status_code == 200
    assert "secret-token" not in listed.text
    assert os.path.isabs(paths.bindings_file)

