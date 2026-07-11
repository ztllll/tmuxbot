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
    setup_token: str | None = None
    session_ttl_seconds: int = 86_400

    @classmethod
    def from_env(cls) -> "WebSettings":
        project_dir = Path(__file__).resolve().parents[2]
        data_dir = Path(os.getenv("TMUXBOT_DATA_DIR") or project_dir / "data")
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
        if setup_token is not None and len(setup_token) < 24:
            raise ValueError(
                "TMUXBOT_WEB_SETUP_TOKEN must be at least 24 characters"
            )
        return cls(
            host=os.getenv("TMUXBOT_WEB_HOST", "127.0.0.1").strip(),
            port=port,
            database_path=data_dir / "control-plane.sqlite3",
            secure_cookie=os.getenv("TMUXBOT_WEB_SECURE_COOKIE", "").strip().lower()
            in TRUE_VALUES,
            setup_token=setup_token,
        )
