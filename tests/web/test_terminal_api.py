import asyncio
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from tmuxbot.control_plane.repository import ControlPlaneRepository
from tmuxbot.control_plane.models import TmuxPaneRecord
from tmuxbot.state import Binding
from tmuxbot.web.__main__ import build_terminal_service
from tmuxbot.web.app import create_app
from tmuxbot.web.settings import WebSettings
from tmuxbot.web.terminal import TerminalService


PASSWORD = "correct horse battery staple"
SETUP_TOKEN = "0123456789abcdef0123456789abcdef"


class FakeInventory:
    def __init__(self, panes=None):
        self.panes = panes or []

    def list_panes(self):
        return list(self.panes)


class FakeTerminal:
    def __init__(self):
        self.output = asyncio.Queue()
        self.writes = []
        self.resizes = []
        self.closed = False

    async def read(self, max_bytes=65536):
        return await self.output.get()

    async def write(self, data):
        self.writes.append(data)

    async def resize(self, rows, cols):
        self.resizes.append((rows, cols))

    async def close(self):
        self.closed = True


def _app(tmp_path: Path, inventory=None):
    settings = WebSettings(
        host="127.0.0.1",
        port=8765,
        database_path=tmp_path / "control.sqlite3",
        secure_cookie=False,
        setup_token=SETUP_TOKEN,
        session_ttl_seconds=3600,
    )
    repository = ControlPlaneRepository(settings.database_path)
    repository.migrate()
    opened_targets = []
    terminals = []

    async def terminal_factory(target):
        opened_targets.append(target)
        terminal = FakeTerminal()
        terminals.append(terminal)
        await terminal.output.put(b"terminal-ready")
        return terminal

    service = TerminalService(
        repository=repository,
        target_resolver=lambda session_id: {
            "managed-1": "alpha:0.0",
        }.get(session_id),
        allowed_origin="http://testserver",
        terminal_factory=terminal_factory,
    )
    app = create_app(
        settings,
        repository,
        inventory or FakeInventory(),
        [],
        terminal_service=service,
    )
    return app, repository, service, opened_targets, terminals


def _login(client: TestClient) -> str:
    bootstrap = client.get("/api/auth/status").json()["csrf_token"]
    setup = client.post(
        "/api/auth/setup",
        json={"password": PASSWORD},
        headers={"X-CSRF-Token": bootstrap, "X-Setup-Token": SETUP_TOKEN},
    )
    assert setup.status_code == 201
    return setup.json()["csrf_token"]


def _new_login(client: TestClient) -> str:
    bootstrap = client.get("/api/auth/status").json()["csrf_token"]
    response = client.post(
        "/api/auth/login",
        json={"password": PASSWORD},
        headers={"X-CSRF-Token": bootstrap},
    )
    assert response.status_code == 200
    return response.json()["csrf_token"]


def _ticket(client: TestClient, csrf: str):
    response = client.post(
        "/api/terminals/managed-1/ticket",
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 201
    body = response.json()
    assert set(body) == {"ticket", "expires_at"}
    assert "alpha:0.0" not in response.text
    return body["ticket"]


def _client(app) -> TestClient:
    return TestClient(app, client=("127.0.0.1", 50000))


def test_terminal_ticket_api_requires_auth_csrf_and_server_side_session_id(tmp_path):
    app, _, _, _, _ = _app(tmp_path)
    client = _client(app)

    assert client.post("/api/terminals/managed-1/ticket").status_code == 401
    csrf = _login(client)
    assert client.post("/api/terminals/managed-1/ticket").status_code == 403
    assert client.post(
        "/api/terminals/managed-1/takeover",
        headers={"X-CSRF-Token": csrf},
    ).status_code == 409
    assert client.post(
        "/api/terminals/not-managed/ticket",
        headers={"X-CSRF-Token": csrf},
    ).status_code == 404


def test_observed_terminal_ticket_only_uses_a_fresh_inventory_target(tmp_path):
    pane = TmuxPaneRecord("loose:1.2", "loose", 1, 2, "bash", "/repo", 99)
    app, _, _, opened_targets, terminals = _app(tmp_path, FakeInventory([pane]))
    client = _client(app)
    csrf = _login(client)

    rejected = client.post(
        "/api/terminals/observed/ticket", json={"target": "browser:0.0"},
        headers={"X-CSRF-Token": csrf},
    )
    assert rejected.status_code == 409
    response = client.post(
        "/api/terminals/observed/ticket", json={"target": pane.target},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 201
    body = response.json()
    assert set(body) == {"terminal_id", "ticket", "expires_at"}
    assert pane.target not in response.text
    with client.websocket_connect(
        f"/api/terminals/ws?ticket={body['ticket']}", headers={"Origin": "http://testserver"}
    ) as websocket:
        assert websocket.receive_bytes() == b"terminal-ready"
    assert opened_targets == [pane.target]
    assert terminals[0].closed is True


def test_web_runtime_resolves_existing_binding_name_to_managed_tmux_target(tmp_path):
    settings = WebSettings(
        host="127.0.0.1",
        port=8765,
        database_path=tmp_path / "control.sqlite3",
        secure_cookie=False,
    )
    repository = ControlPlaneRepository(settings.database_path)
    repository.migrate()
    binding = Binding(
        name="managed-1",
        chat_id=1,
        thread_id=None,
        tmux_session="alpha",
        tmux_window=2,
        tmux_pane=3,
        cwd=tmp_path,
    )

    service = build_terminal_service(settings, repository, [binding])

    assert service.resolve_target("managed-1") == "alpha:2.3"
    assert service.resolve_target("alpha:2.3") is None


def test_terminal_websocket_rejects_missing_or_foreign_origin(tmp_path):
    app, _, _, _, _ = _app(tmp_path)
    client = _client(app)
    csrf = _login(client)

    missing_origin_ticket = _ticket(client, csrf)
    with client.websocket_connect(
        f"/api/terminals/ws?ticket={missing_origin_ticket}"
    ) as websocket:
        try:
            websocket.receive_bytes()
        except WebSocketDisconnect as exc:
            assert exc.code == 4403
        else:
            raise AssertionError("missing Origin must be rejected")

    foreign_origin_ticket = _ticket(client, csrf)
    with client.websocket_connect(
        f"/api/terminals/ws?ticket={foreign_origin_ticket}",
        headers={"Origin": "https://attacker.example"},
    ) as websocket:
        try:
            websocket.receive_bytes()
        except WebSocketDisconnect as exc:
            assert exc.code == 4403
        else:
            raise AssertionError("foreign Origin must be rejected")


def test_terminal_ticket_is_bound_to_authenticated_session_and_single_use(tmp_path):
    app, _, _, _, _ = _app(tmp_path)
    owner = _client(app)
    owner_csrf = _login(owner)
    ticket = _ticket(owner, owner_csrf)

    other = _client(app)
    _new_login(other)
    with other.websocket_connect(
        f"/api/terminals/ws?ticket={ticket}",
        headers={"Origin": "http://testserver"},
    ) as websocket:
        try:
            websocket.receive_bytes()
        except WebSocketDisconnect as exc:
            assert exc.code == 4403
        else:
            raise AssertionError("ticket from another session must be rejected")

    with owner.websocket_connect(
        f"/api/terminals/ws?ticket={ticket}",
        headers={"Origin": "http://testserver"},
    ) as websocket:
        assert websocket.receive_bytes() == b"terminal-ready"

    with owner.websocket_connect(
        f"/api/terminals/ws?ticket={ticket}",
        headers={"Origin": "http://testserver"},
    ) as websocket:
        try:
            websocket.receive_bytes()
        except WebSocketDisconnect as exc:
            assert exc.code == 4403
        else:
            raise AssertionError("used ticket must be rejected")


def test_terminal_defaults_to_observe_then_accepts_input_and_resize_after_takeover(
    tmp_path,
):
    app, repository, _, opened_targets, terminals = _app(tmp_path)
    client = _client(app)
    csrf = _login(client)
    ticket = _ticket(client, csrf)

    with client.websocket_connect(
        f"/api/terminals/ws?ticket={ticket}",
        headers={"Origin": "http://testserver"},
    ) as websocket:
        assert websocket.receive_bytes() == b"terminal-ready"
        assert opened_targets == ["alpha:0.0"]

        websocket.send_bytes(b"blocked")
        assert websocket.receive_json() == {
            "type": "input_rejected",
            "reason": "observe_only",
        }
        assert terminals[0].writes == []

        takeover = client.post(
            "/api/terminals/managed-1/takeover",
            headers={"X-CSRF-Token": csrf},
        )
        assert takeover.status_code == 200
        assert takeover.json() == {"mode": "takeover"}

        websocket.send_bytes(b"accepted")
        websocket.send_json({"type": "resize", "rows": 42, "cols": 120})
        for _ in range(100):
            if terminals[0].writes and terminals[0].resizes:
                break
            asyncio.run(asyncio.sleep(0.001))
        assert terminals[0].writes == [b"accepted"]
        assert terminals[0].resizes == [(42, 120)]

        release = client.delete(
            "/api/terminals/managed-1/takeover",
            headers={"X-CSRF-Token": csrf},
        )
        assert release.status_code == 200
        assert release.json() == {"mode": "observe"}

    assert terminals[0].closed is True
    events = repository.list_events(after_sequence=0, limit=20)
    assert [event.event_type for event in events] == [
        "terminal.takeover.started",
        "terminal.takeover.ended",
    ]
    assert all(event.aggregate_id == "managed-1" for event in events)
    assert all(isinstance(event.occurred_at, datetime) for event in events)
    operator_sessions = [event.payload["operator_session"] for event in events]
    assert len(operator_sessions[0]) == 16
    assert operator_sessions == [operator_sessions[0], operator_sessions[0]]


def test_disconnect_ends_takeover_without_killing_tmux_session(tmp_path):
    app, repository, service, _, terminals = _app(tmp_path)
    client = _client(app)
    csrf = _login(client)
    ticket = _ticket(client, csrf)

    with client.websocket_connect(
        f"/api/terminals/ws?ticket={ticket}",
        headers={"Origin": "http://testserver"},
    ) as websocket:
        assert websocket.receive_bytes() == b"terminal-ready"
        assert client.post(
            "/api/terminals/managed-1/takeover",
            headers={"X-CSRF-Token": csrf},
        ).status_code == 200

    assert terminals[0].closed is True
    assert service.takeovers.is_active("managed-1") is False
    events = repository.list_events(after_sequence=0, limit=20)
    assert [event.event_type for event in events] == [
        "terminal.takeover.started",
        "terminal.takeover.ended",
    ]


def test_terminal_rejects_oversized_input_frame(tmp_path):
    app, _, _, _, terminals = _app(tmp_path)
    client = _client(app)
    csrf = _login(client)
    ticket = _ticket(client, csrf)

    with client.websocket_connect(
        f"/api/terminals/ws?ticket={ticket}",
        headers={"Origin": "http://testserver"},
    ) as websocket:
        assert websocket.receive_bytes() == b"terminal-ready"
        assert client.post(
            "/api/terminals/managed-1/takeover",
            headers={"X-CSRF-Token": csrf},
        ).status_code == 200
        websocket.send_bytes(b"x" * 65_537)
        try:
            websocket.receive_bytes()
        except WebSocketDisconnect as exc:
            assert exc.code == 1009
        else:
            raise AssertionError("oversized terminal input must close the socket")

    assert terminals[0].writes == []


def test_terminal_rejects_out_of_range_resize_without_touching_pty(tmp_path):
    app, _, _, _, terminals = _app(tmp_path)
    client = _client(app)
    csrf = _login(client)
    ticket = _ticket(client, csrf)

    with client.websocket_connect(
        f"/api/terminals/ws?ticket={ticket}",
        headers={"Origin": "http://testserver"},
    ) as websocket:
        assert websocket.receive_bytes() == b"terminal-ready"
        websocket.send_json({"type": "resize", "rows": 0, "cols": 120})
        assert websocket.receive_json() == {
            "type": "message_rejected",
            "reason": "invalid_frame",
        }

    assert terminals[0].resizes == []


def test_disconnect_closes_attach_client_even_when_end_audit_fails(
    tmp_path, monkeypatch
):
    app, repository, _, _, terminals = _app(tmp_path)
    client = _client(app)
    csrf = _login(client)
    ticket = _ticket(client, csrf)

    with pytest.raises(RuntimeError, match="audit unavailable"):
        with client.websocket_connect(
            f"/api/terminals/ws?ticket={ticket}",
            headers={"Origin": "http://testserver"},
        ) as websocket:
            assert websocket.receive_bytes() == b"terminal-ready"
            assert client.post(
                "/api/terminals/managed-1/takeover",
                headers={"X-CSRF-Token": csrf},
            ).status_code == 200
            monkeypatch.setattr(
                repository,
                "append_event",
                lambda _event: (_ for _ in ()).throw(
                    RuntimeError("audit unavailable")
                ),
            )

    assert terminals[0].closed is True
