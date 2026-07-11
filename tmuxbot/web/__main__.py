from __future__ import annotations

import os
from pathlib import Path

import uvicorn
from dotenv import load_dotenv

from tmuxbot.config import load_config
from tmuxbot.control_plane.repository import ControlPlaneRepository
from tmuxbot.control_plane.tmux_inventory import TmuxInventory
from tmuxbot.paths import RuntimePaths
from tmuxbot.state import S
from tmuxbot.web.app import create_app
from tmuxbot.web.settings import WebSettings


def build_app():
    legacy_project_dir = Path(__file__).resolve().parents[2]
    paths = RuntimePaths.discover(
        os.environ, legacy_project_dir=legacy_project_dir
    )
    load_dotenv(paths.env_file, override=False)
    paths = RuntimePaths.discover(
        os.environ, legacy_project_dir=legacy_project_dir
    )
    paths.ensure_private_directories()
    load_config(
        paths.env_file,
        paths.bindings_file,
        paths.offsets_file,
        allow_missing_bindings=True,
        allow_empty_bindings=True,
    )
    settings = WebSettings.from_env(database_path=paths.database_file)
    repository = ControlPlaneRepository(settings.database_path)
    repository.migrate()
    app = create_app(settings, repository, TmuxInventory(), S.bindings)
    return settings, app


def run_web() -> None:
    settings, app = build_app()
    uvicorn.run(app, host=settings.host, port=settings.port, proxy_headers=False)


if __name__ == "__main__":
    run_web()
