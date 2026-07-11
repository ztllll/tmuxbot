from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


TRUE_VALUES = {"1", "true", "yes", "on"}


@dataclass(frozen=True, slots=True)
class WebSettings:
    host: str
    port: int
    database_path: Path
    secure_cookie: bool
    session_ttl_seconds: int = 86_400
    setup_token: str | None = None

    @classmethod
    def from_env(cls, *, database_path: Path | None = None) -> "WebSettings":
        if database_path is None:
            from tmuxbot.paths import RuntimePaths

            database_path = RuntimePaths.discover(os.environ).database_file
        port_value = os.getenv("TMUXBOT_WEB_PORT", "8765").strip()
        try:
            port = int(port_value)
        except ValueError as exc:
            raise ValueError(
                "TMUXBOT_WEB_PORT must be an integer from 1 to 65535"
            ) from exc
        if not 1 <= port <= 65_535:
            raise ValueError("TMUXBOT_WEB_PORT must be an integer from 1 to 65535")
        setup_token_value = os.getenv("TMUXBOT_WEB_SETUP_TOKEN")
        setup_token = setup_token_value.strip() if setup_token_value is not None else None
        if setup_token is not None and (
            len(setup_token) < 24 or not setup_token.isascii()
        ):
            raise ValueError(
                "TMUXBOT_WEB_SETUP_TOKEN must be an ASCII string "
                "at least 24 characters long"
            )
        return cls(
            host=os.getenv("TMUXBOT_WEB_HOST", "127.0.0.1").strip(),
            port=port,
            database_path=database_path,
            secure_cookie=os.getenv("TMUXBOT_WEB_SECURE_COOKIE", "").strip().lower()
            in TRUE_VALUES,
            setup_token=setup_token,
        )
