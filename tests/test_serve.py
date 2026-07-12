import asyncio
from types import SimpleNamespace

from tmuxbot.__main__ import build_parser
from tmuxbot.serve import browser_url, serve


def test_cli_exposes_serve_and_doctor() -> None:
    parser = build_parser()
    assert parser.parse_args(["serve"]).command == "serve"
    assert parser.parse_args(["serve", "--open"]).open_browser is True
    assert parser.parse_args(["doctor", "--json"]).as_json is True


def test_browser_url_uses_loopback_and_fragment_grant() -> None:
    assert browser_url("0.0.0.0", 8765, "abc") == "http://127.0.0.1:8765/#grant=abc"
    assert "?" not in browser_url("127.0.0.1", 8765, "abc")


def test_service_shutdown_stops_bridge_before_leaving_tmux_panes(monkeypatch):
    stopped = []

    class FakePaths:
        def ensure_private_directories(self):
            return None

    class FakeSupervisor:
        def __init__(self, *_args):
            return None

        def snapshot(self):
            return {"state": "running"}

        async def run(self, stop):
            await stop.wait()

        async def stop(self):
            stopped.append(True)

    class FakeServer:
        def __init__(self, _config):
            self.started = False
            self.should_exit = False

        async def serve(self):
            self.started = True
            while not self.should_exit:
                await asyncio.sleep(0)

    monkeypatch.setattr("tmuxbot.serve.build_app", lambda: (SimpleNamespace(host="127.0.0.1", port=8765), SimpleNamespace(state=SimpleNamespace())))
    monkeypatch.setattr("tmuxbot.serve.RuntimePaths.discover", lambda *_args, **_kwargs: FakePaths())
    monkeypatch.setattr("tmuxbot.serve.BridgeSupervisor", FakeSupervisor)
    monkeypatch.setattr("tmuxbot.serve.uvicorn.Config", lambda *_args, **_kwargs: object())
    monkeypatch.setattr("tmuxbot.serve.uvicorn.Server", FakeServer)

    async def scenario():
        stop = asyncio.Event()
        task = asyncio.create_task(serve(stop_event=stop))
        await asyncio.sleep(0)
        stop.set()
        await task

    asyncio.run(scenario())

    assert stopped == [True]


def test_web_server_exit_also_stops_bridge(monkeypatch):
    stopped = []

    class FakePaths:
        def ensure_private_directories(self):
            return None

    class FakeSupervisor:
        def __init__(self, *_args):
            return None

        def snapshot(self):
            return {"state": "running"}

        async def run(self, stop):
            await stop.wait()

        async def stop(self):
            stopped.append(True)

    class FakeServer:
        def __init__(self, _config):
            self.started = False
            self.should_exit = False

        async def serve(self):
            self.started = True

    monkeypatch.setattr("tmuxbot.serve.build_app", lambda: (SimpleNamespace(host="127.0.0.1", port=8765), SimpleNamespace(state=SimpleNamespace())))
    monkeypatch.setattr("tmuxbot.serve.RuntimePaths.discover", lambda *_args, **_kwargs: FakePaths())
    monkeypatch.setattr("tmuxbot.serve.BridgeSupervisor", FakeSupervisor)
    monkeypatch.setattr("tmuxbot.serve.uvicorn.Config", lambda *_args, **_kwargs: object())
    monkeypatch.setattr("tmuxbot.serve.uvicorn.Server", FakeServer)

    asyncio.run(serve())

    assert stopped == [True]
