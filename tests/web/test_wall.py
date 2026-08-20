import asyncio
from types import SimpleNamespace

from fastapi.testclient import TestClient

from tmuxbot.control_plane.repository import ControlPlaneRepository
from tmuxbot.control_plane.tmux_inventory import TmuxInventory
from tmuxbot.web.app import create_app
from tmuxbot.web.control_mode import _unescape_output
from tmuxbot.web.settings import WebSettings
from tmuxbot.web.wall import TmuxWall


def app(tmp_path):
    settings = WebSettings("127.0.0.1", 8765, tmp_path / "control.sqlite3", False)
    repository = ControlPlaneRepository(settings.database_path)
    repository.migrate()
    return create_app(settings, repository, TmuxInventory(), [])


def tmux_run(output=b"alpha\t0\tbash\t/repo\n"):
    def run(argv, **_kwargs):
        stdout = b"80x24\n" if "display-message" in argv else output
        return SimpleNamespace(returncode=0, stdout=stdout, stderr=b"")
    return run


def test_wall_inventory_groups_panes_by_window(monkeypatch):
    output = b"alpha\t0\tbash\t/repo\nalpha\t0\tpython\t/repo\nalpha\t1\tclaude\t/other\n"
    monkeypatch.setattr("tmuxbot.web.wall.subprocess.run", lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=output, stderr=b""))
    assert [(item.target, item.pane_count, item.commands) for item in TmuxWall().list_windows()] == [("alpha:0", 2, ("bash", "python")), ("alpha:1", 1, ("claude",))]


def test_wall_reads_validated_window_size(monkeypatch):
    monkeypatch.setattr("tmuxbot.web.wall.subprocess.run", lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=b"120x40\n", stderr=b""))
    assert TmuxWall().window_size("alpha:0") == (120, 40)


def test_control_mode_unescapes_tmux_output():
    assert _unescape_output(rb"hello\040world\n\033[31m") == b"hello world\n\x1b[31m"


def test_wall_api_returns_windows(monkeypatch, tmp_path):
    monkeypatch.setattr("tmuxbot.web.wall.subprocess.run", tmux_run())
    response = TestClient(app(tmp_path)).get("/api/tmux/windows")
    assert response.status_code == 200
    assert response.json() == [{"target": "alpha:0", "session_name": "alpha", "window_index": 0, "pane_count": 1, "commands": ["bash"], "cwd_summary": "/repo"}]


def test_control_wall_forwards_raw_input_and_resizes_active_terminal(monkeypatch, tmp_path):
    monkeypatch.setattr("tmuxbot.web.wall.subprocess.run", tmux_run())
    events = []
    class Terminal:
        snapshot = "snapshot"
        async def pump(self): await asyncio.sleep(1)
        async def read(self, _max=65536):
            if not hasattr(self, "ready"):
                self.ready = True
                return b"ready"
            await asyncio.sleep(1)
            return b""
        async def write(self, data): events.append(("write", data))
        async def resize(self, rows, cols): events.append(("resize", rows, cols))
        async def close(self): events.append(("close",))
    async def open_terminal(_target): return Terminal()
    monkeypatch.setattr("tmuxbot.web.app.open_control_terminal", open_terminal)
    with TestClient(app(tmp_path)).websocket_connect("/api/wall/ws?target=alpha:0") as websocket:
        assert websocket.receive_json() == {"type": "snapshot", "data": "snapshot", "cols": 80, "rows": 24}
        assert websocket.receive_bytes() == b"ready"
        websocket.send_bytes(b"hello")
        websocket.send_json({"type": "resize", "rows": 40, "cols": 120})
        for _ in range(30):
            if events: break
            asyncio.run(asyncio.sleep(.001))
    assert ("write", b"hello") in events
    assert ("resize", 40, 120) in events
    assert ("close",) in events
