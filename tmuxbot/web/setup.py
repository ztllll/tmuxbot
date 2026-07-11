from __future__ import annotations

import secrets
from dataclasses import dataclass


SETUP_GRANT_TTL_SECONDS = 600


@dataclass(slots=True)
class SetupGrant:
    token: str
    expires_at: int
    consumed: bool = False

    @classmethod
    def generate(
        cls, *, now: int, ttl_seconds: int = SETUP_GRANT_TTL_SECONDS
    ) -> "SetupGrant":
        return cls(
            token=secrets.token_urlsafe(32),
            expires_at=now + ttl_seconds,
        )

    def is_available(self, *, now: int) -> bool:
        return not self.consumed and now < self.expires_at

    def authorize(self, submitted: str, *, now: int) -> bool:
        token_matches = secrets.compare_digest(submitted, self.token)
        return token_matches and self.is_available(now=now)

    def consume(self) -> None:
        self.consumed = True
