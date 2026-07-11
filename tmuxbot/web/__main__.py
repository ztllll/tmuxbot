from __future__ import annotations

import os
from pathlib import Path

import uvicorn

from tmuxbot.config import load_config
from tmuxbot.control_plane.repository import ControlPlaneRepository
from tmuxbot.control_plane.tmux_inventory import TmuxInventory
from tmuxbot.state import S
from tmuxbot.web.app import create_app
from tmuxbot.web.settings import WebSettings


PROJECT_DIR = Path(__file__).resolve().parents[2]


def runtime_paths() -> tuple[Path, Path, Path]:
    data_dir = Path(os.getenv("TMUXBOT_DATA_DIR") or PROJECT_DIR / "data")
    env_file = Path(os.getenv("TMUXBOT_ENV") or PROJECT_DIR / ".env")
    bindings_file = Path(
        os.getenv("TMUXBOT_BINDINGS") or PROJECT_DIR / "bindings.yaml"
    )
    return env_file, bindings_file, data_dir / "offsets.json"


def build_app():
    load_config(*runtime_paths())
    settings = WebSettings.from_env()
    repository = ControlPlaneRepository(settings.database_path)
    repository.migrate()
    app = create_app(settings, repository, TmuxInventory(), S.bindings)
    return settings, app


def run_web() -> None:
    settings, app = build_app()
    uvicorn.run(app, host=settings.host, port=settings.port, proxy_headers=False)


if __name__ == "__main__":
    run_web()
