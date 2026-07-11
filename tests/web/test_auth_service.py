import hashlib
import secrets
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Lock

import pytest
from pwdlib import PasswordHash

from tmuxbot.control_plane.repository import ControlPlaneRepository
from tmuxbot.web.auth import AuthError, AuthService


NOW = 2_000_000_000


def test_auth_service_requires_one_time_setup_and_rotates_session(tmp_path):
    path = tmp_path / "control.sqlite3"
    repo = ControlPlaneRepository(path)
    repo.migrate()
    auth = AuthService(repo, session_ttl_seconds=3600)

    assert auth.is_configured() is False
    session = auth.setup("correct horse battery staple", now=NOW)
    assert auth.is_configured() is True
    assert auth.authenticate(session.token, now=NOW + 1).csrf_token == session.csrf_token

    password_hash = repo.get_setting(AuthService.PASSWORD_KEY)
    assert password_hash is not None
    assert password_hash.startswith("$argon2id$")
    with sqlite3.connect(path) as db:
        stored_token_hash = db.execute("SELECT token_hash FROM web_sessions").fetchone()[0]
    assert stored_token_hash == hashlib.sha256(session.token.encode()).hexdigest()
    assert stored_token_hash != session.token

    with pytest.raises(AuthError, match="invalid or expired session"):
        auth.authenticate(session.token + "tampered", now=NOW + 1)
    with pytest.raises(AuthError, match="already configured"):
        auth.setup("another acceptable password", now=NOW + 2)
    with pytest.raises(AuthError, match="invalid credentials"):
        auth.login("wrong password", now=NOW + 3)

    replacement = auth.login("correct horse battery staple", now=NOW + 3)
    assert replacement.token != session.token
    assert replacement.csrf_token != session.csrf_token
    assert auth.authenticate(session.token, now=NOW + 4) == session

    auth.logout(session.token)
    with pytest.raises(AuthError, match="invalid or expired session"):
        auth.authenticate(session.token, now=NOW + 4)
    with pytest.raises(AuthError, match="invalid or expired session"):
        auth.authenticate(replacement.token, now=NOW + 3603)


def test_auth_service_rejects_short_password(tmp_path):
    repo = ControlPlaneRepository(tmp_path / "control.sqlite3")
    repo.migrate()
    auth = AuthService(repo, session_ttl_seconds=3600)

    with pytest.raises(AuthError, match="at least 12 characters"):
        auth.setup("too short", now=1000)

    assert auth.is_configured() is False


def test_concurrent_setup_configures_exactly_one_password(tmp_path, monkeypatch):
    repo = ControlPlaneRepository(tmp_path / "control.sqlite3")
    repo.migrate()
    first = AuthService(repo, session_ttl_seconds=3600)
    second = AuthService(repo, session_ttl_seconds=3600)
    passwords = ("first acceptable password", "second acceptable password")
    hashes = {password: PasswordHash.recommended().hash(password) for password in passwords}
    barrier = Barrier(2)

    def synchronized_hash(_password_hash, password):
        barrier.wait()
        return hashes[password]

    monkeypatch.setattr(PasswordHash, "hash", synchronized_hash)

    def configure(auth, password):
        try:
            auth.setup(password, now=1000)
        except AuthError:
            return False
        return True

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(configure, (first, second), passwords))

    assert sorted(results) == [False, True]
    accepted = [password for password, configured in zip(passwords, results) if configured]
    assert len(accepted) == 1
    assert first.login(accepted[0], now=1001)


def test_concurrent_signing_key_creation_keeps_all_sessions_valid(tmp_path, monkeypatch):
    repo = ControlPlaneRepository(tmp_path / "control.sqlite3")
    repo.migrate()
    password = "correct horse battery staple"
    repo.set_setting(AuthService.PASSWORD_KEY, PasswordHash.recommended().hash(password))
    first = AuthService(repo, session_ttl_seconds=3600)
    second = AuthService(repo, session_ttl_seconds=3600)
    barrier = Barrier(2)
    key_lock = Lock()
    generated_keys = iter(("key-from-first-login", "key-from-second-login"))
    original_token_urlsafe = secrets.token_urlsafe

    def synchronized_token_urlsafe(length):
        if length != 48:
            return original_token_urlsafe(length)
        barrier.wait()
        with key_lock:
            return next(generated_keys)

    monkeypatch.setattr("tmuxbot.web.auth.secrets.token_urlsafe", synchronized_token_urlsafe)

    with ThreadPoolExecutor(max_workers=2) as executor:
        sessions = list(
            executor.map(lambda auth: auth.login(password, now=NOW), (first, second))
        )

    verifier = AuthService(repo, session_ttl_seconds=3600)
    assert [
        verifier.authenticate(session.token, now=NOW + 1) for session in sessions
    ] == sessions
