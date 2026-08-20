from __future__ import annotations

import os

import uvicorn

from tmuxbot.control_plane.repository import ControlPlaneRepository
from tmuxbot.control_plane.tmux_inventory import TmuxInventory
from tmuxbot.paths import RuntimePaths
from tmuxbot.web.app import create_app
from tmuxbot.web.settings import WebSettings


def build_app():
    """Build the WebUI without loading IM configuration or starting an IM frontend."""
    paths = RuntimePaths.discover(os.environ)
    settings = WebSettings.from_env(database_path=paths.database_file)
    repository = ControlPlaneRepository(settings.database_path)
    repository.migrate()
    return settings, create_app(settings, repository, TmuxInventory(), [])


def run_web() -> None:
    settings, app = build_app()
    uvicorn.run(app, host=settings.host, port=settings.port, proxy_headers=False)


if __name__ == "__main__":
    run_web()
