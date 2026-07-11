import threading
import time
from pathlib import Path

from tmuxbot.control_plane.repository import ControlPlaneRepository
from tmuxbot.web.__main__ import create_automatic_setup_grant
from tmuxbot.web.auth import AuthService
from tmuxbot.web.setup import SETUP_GRANT_TTL_SECONDS, SetupGrant
from tmuxbot.web.settings import WebSettings


def test_setup_grant_generation_is_long_lived_enough_and_available():
    grant = SetupGrant.generate(now=1_000)

    assert len(grant.token) >= 32
    assert grant.expires_at == 1_000 + SETUP_GRANT_TTL_SECONDS
    assert grant.is_available(now=1_000)
    assert grant.is_available(now=grant.expires_at - 1)
    assert not grant.is_available(now=grant.expires_at)


def test_setup_grant_authorization_uses_constant_time_comparison(monkeypatch):
    grant = SetupGrant(token="expected-token", expires_at=2_000)
    comparisons = []

    def compare(candidate, expected):
        comparisons.append((candidate, expected))
        return candidate == expected

    monkeypatch.setattr("tmuxbot.web.setup.secrets.compare_digest", compare)

    assert grant.authorize("submitted-token", now=1_000) is False
    assert comparisons == [("submitted-token", "expected-token")]


def test_setup_grant_rejects_expired_consumed_and_replayed_authorization():
    grant = SetupGrant(token="one-time-token", expires_at=2_000)

    assert grant.authorize("one-time-token", now=2_000) is False
    assert grant.authorize("wrong-token", now=1_000) is False
    assert grant.authorize("one-time-token", now=1_000) is True

    grant.consume()

    assert grant.consumed is True
    assert grant.is_available(now=1_000) is False
    assert grant.authorize("one-time-token", now=1_000) is False


def test_setup_grant_state_is_shared_across_threads():
    grant = SetupGrant(token="thread-token", expires_at=2_000)
    observed = []

    thread = threading.Thread(
        target=lambda: (grant.consume(), observed.append(grant.consumed))
    )
    thread.start()
    thread.join()

    assert observed == [True]
    assert grant.authorize("thread-token", now=1_000) is False


def test_setup_grant_uses_requested_ttl():
    now = int(time.time())

    grant = SetupGrant.generate(now=now, ttl_seconds=45)

    assert grant.expires_at == now + 45


def _settings(tmp_path: Path, *, setup_token: str | None = None) -> WebSettings:
    return WebSettings(
        host="127.0.0.1",
        port=8765,
        database_path=tmp_path / "control.sqlite3",
        secure_cookie=False,
        setup_token=setup_token,
    )


def test_automatic_setup_grant_is_created_only_for_unconfigured_database(tmp_path):
    settings = _settings(tmp_path)
    repository = ControlPlaneRepository(settings.database_path)
    repository.migrate()

    grant = create_automatic_setup_grant(settings, repository, now=1_000)

    assert grant is not None
    assert grant.expires_at == 1_000 + SETUP_GRANT_TTL_SECONDS

    AuthService(repository, session_ttl_seconds=3600).setup(
        "correct horse battery staple", now=1_000
    )

    assert create_automatic_setup_grant(settings, repository, now=1_001) is None


def test_explicit_legacy_setup_token_disables_automatic_grant(tmp_path):
    settings = _settings(tmp_path, setup_token="0123456789abcdef0123456789abcdef")
    repository = ControlPlaneRepository(settings.database_path)
    repository.migrate()

    assert create_automatic_setup_grant(settings, repository, now=1_000) is None
