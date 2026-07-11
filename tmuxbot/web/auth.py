from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass

from itsdangerous import BadSignature, Signer
from pwdlib import PasswordHash
from pwdlib.exceptions import PwdlibError

from tmuxbot.control_plane.repository import ControlPlaneRepository


class AuthError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class AuthenticatedSession:
    token: str
    csrf_token: str


class AuthService:
    PASSWORD_KEY = "auth.password_hash"
    SIGNING_KEY = "auth.cookie_signing_key"

    def __init__(self, repository: ControlPlaneRepository, *, session_ttl_seconds: int):
        self.repository = repository
        self.session_ttl_seconds = session_ttl_seconds
        self.password_hash = PasswordHash.recommended()

    @staticmethod
    def _token_hash(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def is_configured(self) -> bool:
        return self.repository.get_setting(self.PASSWORD_KEY) is not None

    def setup(self, password: str, *, now: int) -> AuthenticatedSession:
        if self.is_configured():
            raise AuthError("password is already configured")
        self._validate_password(password)
        encoded = self.password_hash.hash(password)
        if not self.repository.set_setting_if_absent(self.PASSWORD_KEY, encoded):
            raise AuthError("password is already configured")
        return self._new_session(now)

    def login(self, password: str, *, now: int) -> AuthenticatedSession:
        encoded = self.repository.get_setting(self.PASSWORD_KEY)
        try:
            verified = encoded is not None and self.password_hash.verify(password, encoded)
        except PwdlibError as exc:
            raise AuthError("invalid credentials") from exc
        if not verified:
            raise AuthError("invalid credentials")
        return self._new_session(now)

    def authenticate(self, token: str, *, now: int) -> AuthenticatedSession:
        try:
            self._signer().unsign(token)
        except BadSignature as exc:
            raise AuthError("invalid or expired session") from exc
        csrf = self.repository.get_session(self._token_hash(token), now=now)
        if csrf is None:
            raise AuthError("invalid or expired session")
        return AuthenticatedSession(token=token, csrf_token=csrf)

    def logout(self, token: str) -> None:
        self.repository.delete_session(self._token_hash(token))

    def _new_session(self, now: int) -> AuthenticatedSession:
        token = self._signer().sign(secrets.token_urlsafe(32)).decode("utf-8")
        csrf = secrets.token_urlsafe(24)
        self.repository.create_session(
            self._token_hash(token),
            csrf,
            expires_at=now + self.session_ttl_seconds,
        )
        return AuthenticatedSession(token=token, csrf_token=csrf)

    def _signer(self) -> Signer:
        key = self.repository.get_setting(self.SIGNING_KEY)
        if key is None:
            candidate = secrets.token_urlsafe(48)
            self.repository.set_setting_if_absent(self.SIGNING_KEY, candidate)
            key = self.repository.get_setting(self.SIGNING_KEY)
        if key is None:
            raise AuthError("failed to initialize cookie signing key")
        return Signer(key, salt="tmuxbot-web-session")

    @staticmethod
    def _validate_password(password: str) -> None:
        if len(password) < 12:
            raise AuthError("password must contain at least 12 characters")
