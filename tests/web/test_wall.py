import asyncio
from types import SimpleNamespace

from fastapi.testclient import TestClient

from tmuxbot.control_plane.repository import ControlPlaneRepository
from tmuxbot.control_plane.tmux_inventory import TmuxInventory
from tmuxbot.web.app import create_app
from tmuxbot.web.settings import WebSettings
from tmuxbot.web.wall import TmuxWall


def app(tmp_path):
    settings = WebSettings("127.0.0.1", 8765, tmp_path / "control.sqlite3", False)
    repository = ControlPlaneRepository(settings.database_path)
    repository.migrate()
    return create_app(settings, repository, TmuxInventory(), [])


def test_wall_inventory_groups_panes_by_window(monkeypatch):
    output = b"alpha\t0\tbash\t/repo\nalpha\t0\tpython\t/repo\nalpha\t1\tclaude\t/other\n"
    monkeypatch.setattr("tmuxbot.web.wall.subprocess.run", lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=output, stderr=b""))
    assert [(window.target, window.pane_count, window.commands) for window in TmuxWall().list_windows()] == [("alpha:0", 2, ("bash", "python")), ("alpha:1", 1, ("claude",))]


def test_wall_api_returns_windows(monkeypatch, tmp_path):
    output = b"alpha\t0\tbash\t/repo\n"
    monkeypatch.setattr("tmuxbot.web.wall.subprocess.run", lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=output, stderr=b""))
    response = TestClient(app(tmp_path)).get("/api/tmux/windows")
    assert response.status_code == 200
    assert response.json() == [{"target": "alpha:0", "session_name": "alpha", "window_index": 0, "pane_count": 1, "commands": ["bash"], "cwd_summary": "/repo"}]


def test_wall_websocket_forwards_raw_input_and_resize(monkeypatch, tmp_path):
    output = b"alpha\t0\tbash\t/repo\n"
    monkeypatch.setattr("tmuxbot.web.wall.subprocess.run", lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=output, stderr=b""))
    opened = []
    class Terminal:
        async def read(self, _max=65536):
            if not hasattr(self, "ready"):
                self.ready = True
                return b"ready"
            await asyncio.sleep(1)
            return b""
        async def write(self, data): opened.append(("write", data))
        async def resize(self, rows, cols): opened.append(("resize", rows, cols))
        async def close(self): opened.append(("close",))
    async def open_terminal(_target): return Terminal()
    monkeypatch.setattr("tmuxbot.web.app.open_wall_terminal", open_terminal)
    with TestClient(app(tmp_path)).websocket_connect("/api/wall/ws?target=alpha:0") as websocket:
        assert websocket.receive_bytes() == b"ready"
        websocket.send_bytes(b"hello")
        websocket.send_json({"type": "resize", "rows": 40, "cols": 120})
        for _ in range(30):
            if len(opened) >= 2: break
            asyncio.run(asyncio.sleep(.001))
    assert ("write", b"hello") in opened
    assert ("resize", 40, 120) in opened
    assert ("close",) in opened
