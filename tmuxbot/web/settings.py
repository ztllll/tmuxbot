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

    @classmethod
    def from_env(cls) -> "WebSettings":
        project_dir = Path(__file__).resolve().parents[2]
        data_dir = Path(os.getenv("TMUXBOT_DATA_DIR") or project_dir / "data")
        return cls(
            host=os.getenv("TMUXBOT_WEB_HOST", "127.0.0.1"),
            port=int(os.getenv("TMUXBOT_WEB_PORT", "8765")),
            database_path=data_dir / "control-plane.sqlite3",
            secure_cookie=os.getenv("TMUXBOT_WEB_SECURE_COOKIE", "").lower()
            in TRUE_VALUES,
        )
