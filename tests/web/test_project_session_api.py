from pathlib import Path
from types import SimpleNamespace

from tests.web.test_provider_api import _fake_cli, _make_client


def test_project_and_managed_session_wizard_uses_server_records(
    tmp_path: Path, monkeypatch
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _fake_cli(bin_dir / "codex", "codex 5.0")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PATH", str(bin_dir))
    client, _, csrf = _make_client(tmp_path / "state")
    [provider] = client.post(
        "/api/providers/scan", headers={"X-CSRF-Token": csrf}
    ).json()
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    project = client.post(
        "/api/projects",
        json={"name": "演示项目", "root_path": str(project_dir)},
        headers={"X-CSRF-Token": csrf},
    )
    assert project.status_code == 201

    observed: list[list[str]] = []
    monkeypatch.setattr("tmuxbot.web.app.shutil.which", lambda name: "/usr/bin/tmux")

    def run(argv, **kwargs):
        observed.append(argv)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("tmuxbot.web.app.subprocess.run", run)
    session = client.post(
        "/api/managed-sessions",
        json={
            "project_id": project.json()["id"],
            "provider_id": provider["id"],
            "name": "Codex 实施",
            "binary_path": "/tmp/ignored",
            "tmux_target": "browser-controlled",
        },
        headers={"X-CSRF-Token": csrf},
    )

    assert session.status_code == 201
    assert session.json()["provider"] == "codex"
    assert observed[0][0:3] == ["/usr/bin/tmux", "new-session", "-d"]
    assert "/tmp/ignored" not in observed[0]
    assert "browser-controlled" not in observed[0]
    assert client.get("/api/projects").json()[0]["name"] == "演示项目"
    assert client.get("/api/managed-sessions").json()[0]["name"] == "Codex 实施"

    released = client.delete(
        f"/api/managed-sessions/{session.json()['id']}",
        headers={"X-CSRF-Token": csrf},
    )
    assert released.status_code == 204
    assert client.get("/api/managed-sessions").json() == []


def test_projects_can_be_renamed_repathed_and_deleted(tmp_path: Path) -> None:
    client, _, csrf = _make_client(tmp_path / "state")
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    created = client.post(
        "/api/projects", json={"name": "旧名称", "root_path": str(first)},
        headers={"X-CSRF-Token": csrf},
    )
    project_id = created.json()["id"]

    updated = client.patch(
        f"/api/projects/{project_id}",
        json={"name": "新名称", "root_path": str(second)},
        headers={"X-CSRF-Token": csrf},
    )

    assert updated.status_code == 200
    assert updated.json()["name"] == "新名称"
    assert updated.json()["root_path"] == str(second)
    deleted = client.delete(
        f"/api/projects/{project_id}", headers={"X-CSRF-Token": csrf}
    )
    assert deleted.status_code == 204
    assert client.get("/api/projects").json() == []


def test_project_inspection_validates_directory_before_the_wizard_advances(
    tmp_path: Path, monkeypatch
) -> None:
    client, _, csrf = _make_client(tmp_path / "state")
    project_dir = tmp_path / "project"
    project_dir.mkdir()

    def run(argv, **kwargs):
        if argv[-1] == "--show-toplevel":
            return SimpleNamespace(returncode=0, stdout=f"{project_dir}\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="main\n", stderr="")

    monkeypatch.setattr("tmuxbot.web.app.subprocess.run", run)
    response = client.post(
        "/api/projects/inspect", json={"root_path": str(project_dir)},
        headers={"X-CSRF-Token": csrf},
    )

    assert response.status_code == 200
    assert response.json()["root_path"] == str(project_dir)
    assert response.json()["git_root"] == str(project_dir)
    assert response.json()["branch"] == "main"
