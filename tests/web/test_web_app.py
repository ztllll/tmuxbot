from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner

from tmuxbot.control_plane.models import RunEvent, TmuxPaneRecord
from tmuxbot.control_plane.repository import ControlPlaneRepository
from tmuxbot.state import Binding
from tmuxbot.web.app import BOOTSTRAP_COOKIE_NAME, COOKIE_NAME, create_app
from tmuxbot.web.auth import AuthError, AuthService
from tmuxbot.web.settings import WebSettings


PASSWORD = "correct horse battery staple"


class FakeInventory:
    def __init__(self, panes: list[TmuxPaneRecord] | None = None):
        self.panes = panes or []
        self.list_calls = 0

    def list_panes(self) -> list[TmuxPaneRecord]:
        self.list_calls += 1
        return list(self.panes)


def _settings(tmp_path: Path, *, secure_cookie: bool = False) -> WebSettings:
    return WebSettings(
        host="127.0.0.1",
        port=8765,
        database_path=tmp_path / "control.sqlite3",
        secure_cookie=secure_cookie,
        session_ttl_seconds=3600,
    )


def _client(
    tmp_path: Path,
    *,
    secure_cookie: bool = False,
    inventory: FakeInventory | None = None,
    bindings: list[Binding] | None = None,
    client_host: str = "127.0.0.1",
) -> tuple[TestClient, ControlPlaneRepository, FakeInventory]:
    settings = _settings(tmp_path, secure_cookie=secure_cookie)
    repository = ControlPlaneRepository(settings.database_path)
    repository.migrate()
    fake_inventory = inventory or FakeInventory()
    scheme = "https" if secure_cookie else "http"
    client = TestClient(
        create_app(settings, repository, fake_inventory, bindings or []),
        base_url=f"{scheme}://testserver",
        client=(client_host, 50000),
    )
    return client, repository, fake_inventory


def _bootstrap(client: TestClient) -> str:
    response = client.get("/api/auth/status")
    assert response.status_code == 200
    return response.json()["csrf_token"]


def _setup(client: TestClient) -> str:
    bootstrap_csrf = _bootstrap(client)
    response = client.post(
        "/api/auth/setup",
        json={"password": PASSWORD},
        headers={"X-CSRF-Token": bootstrap_csrf},
    )
    assert response.status_code == 201
    return response.json()["csrf_token"]


def test_web_api_requires_auth_and_csrf(tmp_path):
    client, _, _ = _client(tmp_path)

    assert client.get("/api/health").json() == {"status": "ok"}
    status_response = client.get("/api/auth/status")
    assert status_response.json()["configured"] is False
    assert status_response.json()["csrf_token"]
    assert client.get("/api/events").status_code == 401
    assert client.get("/api/tmux/sessions").status_code == 401

    csrf = _setup(client)

    assert client.get("/api/auth/status").json()["configured"] is True
    assert client.get("/api/events").status_code == 200
    assert client.get("/api/tmux/sessions").status_code == 200
    assert client.post("/api/auth/logout").status_code == 403
    assert client.post(
        "/api/auth/logout", headers={"X-CSRF-Token": "wrong-token"}
    ).status_code == 403
    assert client.post(
        "/api/auth/logout", headers={"X-CSRF-Token": csrf}
    ).status_code == 204
    assert client.get("/api/events").status_code == 401


def test_openapi_schema_is_not_exposed(tmp_path):
    client, _, _ = _client(tmp_path)

    assert client.get("/openapi.json").status_code == 404


def test_setup_is_disabled_after_first_password(tmp_path):
    client, _, _ = _client(tmp_path)

    _setup(client)
    bootstrap_csrf = _bootstrap(client)
    response = client.post(
        "/api/auth/setup",
        json={"password": "another correct password"},
        headers={"X-CSRF-Token": bootstrap_csrf},
    )

    assert response.status_code == 409


@pytest.mark.parametrize("client_host", ["127.0.0.1", "127.255.255.254", "::1"])
def test_setup_allows_ipv4_and_ipv6_loopback_clients(tmp_path, client_host):
    client, _, _ = _client(tmp_path, client_host=client_host)

    assert _setup(client)


def test_setup_rejects_lan_client_without_leaking_details(tmp_path):
    client, _, _ = _client(tmp_path, client_host="192.168.1.25")
    submitted_password = "lan client secret password"
    bootstrap_csrf = _bootstrap(client)

    response = client.post(
        "/api/auth/setup",
        json={"password": submitted_password},
        headers={
            "X-CSRF-Token": bootstrap_csrf,
            "X-Forwarded-For": "127.0.0.1",
        },
    )

    assert response.status_code == 403
    assert response.json() == {"detail": "setup is only allowed from loopback"}
    assert submitted_password not in response.text
    assert client.get("/api/auth/status").json()["configured"] is False


def test_configured_login_remains_available_to_lan_clients(tmp_path):
    loopback_client, _, _ = _client(tmp_path)
    _setup(loopback_client)
    lan_client = TestClient(
        loopback_client.app,
        base_url="http://testserver",
        client=("192.168.1.25", 50000),
    )
    bootstrap_csrf = _bootstrap(lan_client)

    response = lan_client.post(
        "/api/auth/login",
        json={"password": PASSWORD},
        headers={"X-CSRF-Token": bootstrap_csrf},
    )

    assert response.status_code == 200
    assert response.json()["csrf_token"]


def test_login_succeeds_and_failure_is_generic(tmp_path):
    client, _, _ = _client(tmp_path)
    csrf = _setup(client)
    assert client.post(
        "/api/auth/logout", headers={"X-CSRF-Token": csrf}
    ).status_code == 204

    bootstrap_csrf = _bootstrap(client)
    failure = client.post(
        "/api/auth/login",
        json={"password": "wrong password!"},
        headers={"X-CSRF-Token": bootstrap_csrf},
    )
    success = client.post(
        "/api/auth/login",
        json={"password": PASSWORD},
        headers={"X-CSRF-Token": bootstrap_csrf},
    )

    assert failure.status_code == 401
    assert failure.json() == {"detail": "invalid credentials"}
    assert success.status_code == 200
    assert success.json()["csrf_token"]
    assert client.get("/api/events").status_code == 200


def test_password_validation_error_does_not_echo_submitted_password(tmp_path):
    client, _, _ = _client(tmp_path)
    submitted_password = "shortsecret"
    bootstrap_csrf = _bootstrap(client)

    response = client.post(
        "/api/auth/setup",
        json={"password": submitted_password},
        headers={"X-CSRF-Token": bootstrap_csrf},
    )

    assert response.status_code == 422
    assert response.json() == {"detail": "invalid request"}
    assert submitted_password not in response.text


def test_setup_requires_bootstrap_double_submit_csrf(tmp_path):
    client, _, _ = _client(tmp_path)

    missing = client.post("/api/auth/setup", json={"password": PASSWORD})
    bootstrap_csrf = _bootstrap(client)
    missing_header = client.post("/api/auth/setup", json={"password": PASSWORD})
    wrong = client.post(
        "/api/auth/setup",
        json={"password": PASSWORD},
        headers={"X-CSRF-Token": "wrong-token"},
    )
    success = client.post(
        "/api/auth/setup",
        json={"password": PASSWORD},
        headers={"X-CSRF-Token": bootstrap_csrf},
    )

    assert missing.status_code == 403
    assert missing_header.status_code == 403
    assert wrong.status_code == 403
    assert success.status_code == 201
    assert BOOTSTRAP_COOKIE_NAME not in client.cookies


def test_setup_rejects_tampered_bootstrap_csrf_signature(tmp_path):
    client, _, _ = _client(tmp_path)
    bootstrap_csrf = _bootstrap(client)
    tampered = bootstrap_csrf + "tampered"
    client.cookies.set(BOOTSTRAP_COOKIE_NAME, tampered)

    response = client.post(
        "/api/auth/setup",
        json={"password": PASSWORD},
        headers={"X-CSRF-Token": tampered},
    )

    assert response.status_code == 403
    assert response.json() == {"detail": "invalid csrf token"}


def test_setup_rejects_expired_bootstrap_csrf_signature(tmp_path, monkeypatch):
    now = 2_000_000_000
    monkeypatch.setattr(TimestampSigner, "get_timestamp", lambda self: now)
    client, _, _ = _client(tmp_path)
    bootstrap_csrf = _bootstrap(client)
    now += 301

    response = client.post(
        "/api/auth/setup",
        json={"password": PASSWORD},
        headers={"X-CSRF-Token": bootstrap_csrf},
    )

    assert response.status_code == 403
    assert response.json() == {"detail": "invalid csrf token"}


def test_login_requires_bootstrap_double_submit_csrf(tmp_path):
    client, _, _ = _client(tmp_path)
    session_csrf = _setup(client)
    assert client.post(
        "/api/auth/logout", headers={"X-CSRF-Token": session_csrf}
    ).status_code == 204

    missing = client.post("/api/auth/login", json={"password": PASSWORD})
    bootstrap_csrf = _bootstrap(client)
    wrong = client.post(
        "/api/auth/login",
        json={"password": PASSWORD},
        headers={"X-CSRF-Token": "wrong-token"},
    )
    success = client.post(
        "/api/auth/login",
        json={"password": PASSWORD},
        headers={"X-CSRF-Token": bootstrap_csrf},
    )

    assert missing.status_code == 403
    assert wrong.status_code == 403
    assert success.status_code == 200
    assert BOOTSTRAP_COOKIE_NAME not in client.cookies


@pytest.mark.parametrize(
    ("secure_cookie", "secure_attribute"),
    [(False, False), (True, True)],
)
def test_bootstrap_cookie_has_strict_security_attributes(
    tmp_path, secure_cookie, secure_attribute
):
    client, _, _ = _client(tmp_path, secure_cookie=secure_cookie)

    response = client.get("/api/auth/status")

    token = response.json()["csrf_token"]
    cookie = response.headers["set-cookie"].lower()
    assert len(token) >= 32
    assert f"{BOOTSTRAP_COOKIE_NAME}={token}" in response.headers["set-cookie"]
    assert "httponly" in cookie
    assert "samesite=strict" in cookie
    assert "max-age=300" in cookie
    assert "path=/" in cookie
    assert ("secure" in cookie) is secure_attribute


@pytest.mark.parametrize(
    ("secure_cookie", "secure_attribute"),
    [(False, False), (True, True)],
)
def test_session_cookie_has_required_security_attributes(
    tmp_path, secure_cookie, secure_attribute
):
    client, _, _ = _client(tmp_path, secure_cookie=secure_cookie)

    bootstrap_csrf = _bootstrap(client)
    response = client.post(
        "/api/auth/setup",
        json={"password": PASSWORD},
        headers={"X-CSRF-Token": bootstrap_csrf},
    )

    cookie = response.headers["set-cookie"].lower()
    assert f"{COOKIE_NAME}=" in cookie
    assert "httponly" in cookie
    assert "samesite=lax" in cookie
    assert "max-age=3600" in cookie
    assert "path=/" in cookie
    assert ("secure" in cookie) is secure_attribute


def test_state_changing_request_rejects_foreign_origin(tmp_path):
    client, _, _ = _client(tmp_path)

    response = client.post(
        "/api/auth/setup",
        json={"password": PASSWORD},
        headers={"Origin": "https://attacker.example"},
    )

    assert response.status_code == 403
    assert response.json() == {"detail": "invalid origin"}
    assert client.get("/api/auth/status").json()["configured"] is False


def test_configured_public_origin_is_accepted(tmp_path, monkeypatch):
    monkeypatch.setenv("TMUXBOT_WEB_PUBLIC_ORIGIN", "https://tmuxbot.example/")
    client, _, _ = _client(tmp_path)

    bootstrap_csrf = _bootstrap(client)
    response = client.post(
        "/api/auth/setup",
        json={"password": PASSWORD},
        headers={
            "Origin": "https://tmuxbot.example",
            "X-CSRF-Token": bootstrap_csrf,
        },
    )

    assert response.status_code == 201


def test_authentication_error_does_not_expose_internal_exception(tmp_path, monkeypatch):
    client, _, _ = _client(tmp_path)
    _setup(client)

    def fail_authentication(self, token, *, now):
        raise AuthError("sqlite path and signing key leaked")

    monkeypatch.setattr(AuthService, "authenticate", fail_authentication)

    response = client.get("/api/events")

    assert response.status_code == 401
    assert response.json() == {"detail": "authentication required"}


def test_csrf_validation_uses_constant_time_comparison(tmp_path, monkeypatch):
    client, _, _ = _client(tmp_path)
    csrf = _setup(client)
    comparisons = []

    def compare(candidate, expected):
        comparisons.append((candidate, expected))
        return candidate == expected

    monkeypatch.setattr("tmuxbot.web.app.secrets.compare_digest", compare)

    response = client.post(
        "/api/auth/logout", headers={"X-CSRF-Token": csrf}
    )

    assert response.status_code == 204
    assert comparisons == [(csrf, csrf)]


def test_events_serialize_only_persisted_run_events(tmp_path):
    client, repository, _ = _client(tmp_path)
    _setup(client)
    occurred_at = datetime(
        2026, 7, 11, 8, 30, tzinfo=timezone(timedelta(hours=8))
    )
    repository.append_event(
        RunEvent(
            event_id="evt-1",
            event_type="session.discovered",
            aggregate_type="session",
            aggregate_id="alpha:0.0",
            payload={"classification": "orphan", "nested": {"label": "孤儿"}},
            occurred_at=occurred_at,
        )
    )

    response = client.get("/api/events", params={"after": 0, "limit": 1})

    assert response.status_code == 200
    assert response.json() == [
        {
            "sequence": 1,
            "event_id": "evt-1",
            "event_type": "session.discovered",
            "aggregate_type": "session",
            "aggregate_id": "alpha:0.0",
            "payload": {"classification": "orphan", "nested": {"label": "孤儿"}},
            "occurred_at": occurred_at.isoformat(),
        }
    ]


@pytest.mark.parametrize(
    ("limit", "error_type"),
    [(0, "greater_than_equal"), (501, "less_than_equal")],
)
def test_events_reject_limit_outside_supported_range_without_echoing_input(
    tmp_path, limit, error_type
):
    client, _, _ = _client(tmp_path)
    _setup(client)

    response = client.get("/api/events", params={"limit": limit})

    assert response.status_code == 422
    [detail] = response.json()["detail"]
    assert detail["loc"] == ["query", "limit"]
    assert detail["type"] == error_type
    assert "input" not in detail


def test_tmux_inventory_serializes_managed_and_orphan_panes(tmp_path):
    inventory = FakeInventory(
        [
            TmuxPaneRecord(
                target="alpha:0.1",
                session_name="alpha",
                window_index=0,
                pane_index=1,
                command="python",
                cwd="/repo",
                pid=4321,
            ),
            TmuxPaneRecord(
                target="loose:2.0",
                session_name="loose",
                window_index=2,
                pane_index=0,
                command="bash",
                cwd="/tmp",
                pid=99,
            ),
        ]
    )
    binding = Binding(
        name="codex-main",
        chat_id=1,
        thread_id=None,
        tmux_session="alpha",
        tmux_window=0,
        tmux_pane=1,
        cwd=Path("/repo"),
        backend="codex",
    )
    client, _, _ = _client(tmp_path, inventory=inventory, bindings=[binding])
    _setup(client)

    response = client.get("/api/tmux/sessions")

    assert response.status_code == 200
    assert response.json() == [
        {
            "target": "alpha:0.1",
            "session_name": "alpha",
            "window_index": 0,
            "pane_index": 1,
            "command": "python",
            "cwd": "/repo",
            "pid": 4321,
            "classification": "managed",
            "binding_name": "codex-main",
            "provider": "codex",
        },
        {
            "target": "loose:2.0",
            "session_name": "loose",
            "window_index": 2,
            "pane_index": 0,
            "command": "bash",
            "cwd": "/tmp",
            "pid": 99,
            "classification": "orphan",
            "binding_name": None,
            "provider": None,
        },
    ]
    assert inventory.list_calls == 1
