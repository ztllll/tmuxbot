from __future__ import annotations

import os
import time
from pathlib import Path

import uvicorn
from dotenv import load_dotenv

from tmuxbot.config import load_config
from tmuxbot.control_plane.repository import ControlPlaneRepository
from tmuxbot.control_plane.tmux_inventory import TmuxInventory
from tmuxbot.paths import RuntimePaths
from tmuxbot.state import S
from tmuxbot.web.app import create_app
from tmuxbot.web.auth import AuthService
from tmuxbot.web.setup import SetupGrant
from tmuxbot.web.settings import WebSettings


def create_automatic_setup_grant(
    settings: WebSettings,
    repository: ControlPlaneRepository,
    *,
    now: int | None = None,
) -> SetupGrant | None:
    if not hasattr(settings, "setup_token") or settings.setup_token is not None:
        return None
    auth = AuthService(
        repository, session_ttl_seconds=settings.session_ttl_seconds
    )
    if auth.is_configured():
        return None
    return SetupGrant.generate(now=int(time.time()) if now is None else now)


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
    setup_grant = create_automatic_setup_grant(settings, repository)
    if setup_grant is None:
        app = create_app(settings, repository, TmuxInventory(), S.bindings)
    else:
        app = create_app(
            settings,
            repository,
            TmuxInventory(),
            S.bindings,
            setup_grant=setup_grant,
        )
    return settings, app


def run_web() -> None:
    settings, app = build_app()
    uvicorn.run(app, host=settings.host, port=settings.port, proxy_headers=False)


if __name__ == "__main__":
    run_web()
