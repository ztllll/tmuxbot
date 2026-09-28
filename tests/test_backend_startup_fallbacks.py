"""Route-startup fallbacks: missing cwd, missing pi session, missing provider binary."""

import asyncio
import json
from pathlib import Path

import pytest

from tmuxbot.backends.base import Backend, BackendUnavailable
from tmuxbot.backends.pi import PiBackend, encode_pi_cwd
from tmuxbot.lifecycle import ensure_binding_running
from tmuxbot.state import Binding
from tmuxbot.tmux import tmux_new_session

import tmuxbot.backends.pi as pi


def binding(cwd: Path) -> Binding:
    return Binding(
        name="pi-route",
        chat_id=1,
        thread_id=None,
        tmux_session="pi-route",
        tmux_window=0,
        tmux_pane=0,
        cwd=cwd,
        backend="pi",
    )


def write_session(path: Path, cwd: Path, session_id: str = "session-1") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"type": "session", "version": 3, "id": session_id, "cwd": str(cwd)})
        + "\n"
    )


def test_tmux_new_session_creates_missing_cwd(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        "tmuxbot.tmux._tmux",
        lambda *args: calls.append(args),
    )
    missing = tmp_path / "brand-new-workspace"
    tmux_new_session("route", missing)
    assert missing.is_dir()
    assert calls == [("new-session", "-d", "-s", "route", "-c", str(missing))]


def test_pi_ensure_running_downgrades_when_session_file_absent(tmp_path, monkeypatch):
    route = binding(tmp_path)
    route.provider_session_id = "01a0dead-beef-0000-0000-000000000000"
    sessions_root = tmp_path / "sessions"
    monkeypatch.setattr(pi, "PI_SESSIONS_DIR", sessions_root)
    monkeypatch.setattr(pi, "tmux_has_session", lambda _session: True)
    commands = iter(["bash", "pi", "pi", "pi"])
    monkeypatch.setattr(pi, "tmux_pane_command", lambda _target: next(commands, "pi"))
    launched: list[str] = []

    async def safe_launch(target, command, *, allowed_shells):
        launched.append(command)
        return True

    async def fast_sleep(_seconds):
        return None

    monkeypatch.setattr(pi, "tmux_safe_launch", safe_launch)
    monkeypatch.setattr(pi, "tmux_capture", lambda *_args: "")
    status = type("S", (), {"model": "m", "context_limit": None, "extension_statuses": []})()
    monkeypatch.setattr(PiBackend, "parse_terminal_status", lambda _self, _pane: status)
    monkeypatch.setattr(pi.asyncio, "sleep", fast_sleep)

    asyncio.run(PiBackend().ensure_running(route))

    assert launched and "--session" not in launched[0]


def test_pi_ensure_running_resumes_existing_session_file(tmp_path, monkeypatch):
    route = binding(tmp_path)
    session_id = "01a0c8ea-2229-70a4-9d73-cc5e49d51822"
    route.provider_session_id = session_id
    sessions_root = tmp_path / "sessions"
    write_session(
        sessions_root / encode_pi_cwd(route.cwd) / f"{session_id}.jsonl",
        route.cwd,
        session_id=session_id,
    )
    monkeypatch.setattr(pi, "PI_SESSIONS_DIR", sessions_root)
    monkeypatch.setattr(pi, "tmux_has_session", lambda _session: True)
    commands = iter(["bash", "pi", "pi", "pi"])
    monkeypatch.setattr(pi, "tmux_pane_command", lambda _target: next(commands, "pi"))
    launched: list[str] = []

    async def safe_launch(target, command, *, allowed_shells):
        launched.append(command)
        return True

    async def fast_sleep(_seconds):
        return None

    monkeypatch.setattr(pi, "tmux_safe_launch", safe_launch)
    monkeypatch.setattr(pi, "tmux_capture", lambda *_args: "footer")
    status = type("S", (), {"model": "m", "context_limit": None, "extension_statuses": []})()
    monkeypatch.setattr(PiBackend, "parse_terminal_status", lambda _self, _pane: status)
    monkeypatch.setattr(pi.asyncio, "sleep", fast_sleep)

    asyncio.run(PiBackend().ensure_running(route))

    assert launched and f"--session {session_id}" in launched[0]


class _FakeBackend(Backend):
    name = "pi"
    pane_command_name = "pi"

    def __init__(self, binary_name="pi"):
        self._binary_name = binary_name
        self.ensured = 0

    @property
    def provider_binary_name(self):
        return self._binary_name

    async def ensure_running(self, b):
        self.ensured += 1

    def find_active_jsonl(self, b):
        return None

    def parse_event(self, line, provider_session_id=None):
        return []

    def find_tui_activity_fp(self, pane):
        return None

    @property
    def command_opts(self):
        return {}

    @property
    def command_aliases(self):
        return {}


class _State:
    def __init__(self):
        self.ensure_locks = {}


def test_missing_binary_skips_new_route_without_calling_ensure(tmp_path, monkeypatch):
    route = binding(tmp_path)
    backend = _FakeBackend()
    monkeypatch.setattr("tmuxbot.tmux.tmux_has_session", lambda _s: False)
    monkeypatch.setattr(
        "tmuxbot.providers.discovery.ProviderDiscovery.resolve_executable",
        staticmethod(lambda _name: None),
    )

    with pytest.raises(BackendUnavailable):
        asyncio.run(
            ensure_binding_running(backend, route, _State(), reason="test", wait=True)
        )
    assert backend.ensured == 0


def test_available_binary_starts_new_route(tmp_path, monkeypatch):
    route = binding(tmp_path)
    backend = _FakeBackend()
    monkeypatch.setattr("tmuxbot.tmux.tmux_has_session", lambda _s: False)
    monkeypatch.setattr(
        "tmuxbot.providers.discovery.ProviderDiscovery.resolve_executable",
        staticmethod(lambda _name: str(tmp_path / "pi")),
    )
    (tmp_path / "pi").write_text("#!/bin/sh\n")

    asyncio.run(ensure_binding_running(backend, route, _State(), reason="test", wait=True))
    assert backend.ensured == 1


def test_existing_pane_never_prechecked_even_without_binary(tmp_path, monkeypatch):
    route = binding(tmp_path)
    backend = _FakeBackend()
    monkeypatch.setattr("tmuxbot.tmux.tmux_has_session", lambda _s: True)
    monkeypatch.setattr(
        "tmuxbot.providers.discovery.ProviderDiscovery.resolve_executable",
        staticmethod(lambda _name: None),
    )

    asyncio.run(ensure_binding_running(backend, route, _State(), reason="test", wait=True))
    assert backend.ensured == 1
