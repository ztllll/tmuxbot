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


def test_wall_attach_ignores_browser_size(monkeypatch):
    from tmuxbot.web.wall import PtyTerminal

    master_fd, slave_fd = 10, 11
    class Process:
        def poll(self): return 0
    calls = []
    monkeypatch.setattr("tmuxbot.web.wall.pty.openpty", lambda: (master_fd, slave_fd))
    monkeypatch.setattr("tmuxbot.web.wall.os.close", lambda _fd: None)
    monkeypatch.setattr("tmuxbot.web.wall.subprocess.Popen", lambda argv, **kwargs: calls.append(argv) or Process())

    PtyTerminal.open("alpha:0")

    assert calls == [["tmux", "attach-session", "-f", "ignore-size", "-t", "alpha:0"]]


def test_wall_reads_validated_window_size(monkeypatch):
    monkeypatch.setattr(
        "tmuxbot.web.wall.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=b"120x40\n", stderr=b""),
    )

    assert TmuxWall().window_size("alpha:0") == (120, 40)


def test_pty_resize_updates_attach_tty_and_real_tmux_window(monkeypatch):
    from tmuxbot.web.wall import PtyTerminal

    calls = []
    monkeypatch.setattr("tmuxbot.web.wall.fcntl.ioctl", lambda *args: calls.append(("ioctl", args)))
    monkeypatch.setattr("tmuxbot.web.wall.subprocess.run", lambda argv, **kwargs: calls.append(("tmux", argv, kwargs)) or SimpleNamespace(returncode=0))
    terminal = PtyTerminal(99, SimpleNamespace(poll=lambda: 0), "alpha:0")

    asyncio.run(terminal.resize(40, 120))

    assert [kind for kind, *_ in calls] == ["ioctl"]

    asyncio.run(terminal.resize(40, 120, apply_window=True))

    assert calls[2][1] == ["tmux", "resize-window", "-t", "alpha:0", "-x", "120", "-y", "40"]


def test_pty_close_restores_inherited_window_size_policy(monkeypatch):
    from tmuxbot.web.wall import PtyTerminal

    commands = []
    monkeypatch.setattr("tmuxbot.web.wall.os.close", lambda _fd: None)
    monkeypatch.setattr(
        "tmuxbot.web.wall.subprocess.run",
        lambda argv, **kwargs: commands.append(argv) or SimpleNamespace(returncode=0),
    )
    terminal = PtyTerminal(99, SimpleNamespace(poll=lambda: 0), "alpha:0")

    asyncio.run(terminal.close())

    assert commands == []


def test_pty_close_restores_size_policy_only_after_size_takeover(monkeypatch):
    from tmuxbot.web.wall import PtyTerminal

    commands = []
    monkeypatch.setattr("tmuxbot.web.wall.os.close", lambda _fd: None)
    monkeypatch.setattr("tmuxbot.web.wall.subprocess.run", lambda argv, **kwargs: commands.append(argv) or SimpleNamespace(returncode=0))
    terminal = PtyTerminal(99, SimpleNamespace(poll=lambda: 0), "alpha:0")
    terminal.applied_window_size = True

    asyncio.run(terminal.close())

    assert commands == [["tmux", "set-window-option", "-u", "-t", "alpha:0", "window-size"]]


def test_wall_api_returns_windows(monkeypatch, tmp_path):
    output = b"alpha\t0\tbash\t/repo\n"
    monkeypatch.setattr("tmuxbot.web.wall.subprocess.run", lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=output, stderr=b""))
    response = TestClient(app(tmp_path)).get("/api/tmux/windows")
    assert response.status_code == 200
    assert response.json() == [{"target": "alpha:0", "session_name": "alpha", "window_index": 0, "pane_count": 1, "commands": ["bash"], "cwd_summary": "/repo"}]


def test_wall_websocket_forwards_raw_input_and_resize(monkeypatch, tmp_path):
    output = b"alpha\t0\tbash\t/repo\n"
    def tmux_run(argv, **kwargs):
        stdout = b"80x24\n" if "display-message" in argv else output
        return SimpleNamespace(returncode=0, stdout=stdout, stderr=b"")
    monkeypatch.setattr("tmuxbot.web.wall.subprocess.run", tmux_run)
    opened = []
    class Terminal:
        async def read(self, _max=65536):
            if not hasattr(self, "ready"):
                self.ready = True
                return b"ready"
            await asyncio.sleep(1)
            return b""
        async def write(self, data): opened.append(("write", data))
        async def resize(self, rows, cols, *, apply_window=False): opened.append(("resize", rows, cols, apply_window))
        async def release_window_size(self): opened.append(("release",))
        async def close(self): opened.append(("close",))
    async def open_terminal(_target): return Terminal()
    monkeypatch.setattr("tmuxbot.web.app.open_wall_terminal", open_terminal)
    with TestClient(app(tmp_path)).websocket_connect("/api/wall/ws?target=alpha:0") as websocket:
        assert websocket.receive_json() == {"type": "window_size", "cols": 80, "rows": 24}
        assert websocket.receive_bytes() == b"ready"
        websocket.send_bytes(b"hello")
        websocket.send_json({"type": "resize", "rows": 40, "cols": 120})
        for _ in range(30):
            if len(opened) >= 2: break
            asyncio.run(asyncio.sleep(.001))
    assert ("write", b"hello") in opened
    assert ("resize", 40, 120, False) in opened
    assert ("close",) in opened


def test_wall_websocket_releases_manual_window_size(monkeypatch, tmp_path):
    output = b"alpha\t0\tbash\t/repo\n"
    def tmux_run(argv, **kwargs):
        stdout = b"80x24\n" if "display-message" in argv else output
        return SimpleNamespace(returncode=0, stdout=stdout, stderr=b"")
    monkeypatch.setattr("tmuxbot.web.wall.subprocess.run", tmux_run)
    opened = []
    class Terminal:
        async def read(self, _max=65536):
            if not hasattr(self, "ready"):
                self.ready = True
                return b"ready"
            await asyncio.sleep(1)
            return b""
        async def write(self, _data): pass
        async def resize(self, _rows, _cols, *, apply_window=False): pass
        async def release_window_size(self): opened.append("released")
        async def close(self): pass
    async def open_terminal(_target): return Terminal()
    monkeypatch.setattr("tmuxbot.web.app.open_wall_terminal", open_terminal)
    with TestClient(app(tmp_path)).websocket_connect("/api/wall/ws?target=alpha:0") as websocket:
        websocket.receive_json(); websocket.receive_bytes()
        websocket.send_json({"type": "release_window_size"})
        for _ in range(30):
            if opened: break
            asyncio.run(asyncio.sleep(.001))
    assert opened == ["released"]
