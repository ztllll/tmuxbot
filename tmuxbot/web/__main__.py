from __future__ import annotations

import os
import time

import uvicorn

from tmuxbot.control_plane.repository import ControlPlaneRepository
from tmuxbot.control_plane.tmux_inventory import TmuxInventory
from tmuxbot.paths import RuntimePaths
from tmuxbot.state import Binding
from tmuxbot.web.app import create_app
from tmuxbot.web.auth import AuthService
from tmuxbot.web.settings import WebSettings
from tmuxbot.web.setup import SetupGrant
from tmuxbot.web.terminal import TerminalService


def create_automatic_setup_grant(
    settings: WebSettings,
    repository: ControlPlaneRepository,
    *,
    now: int | None = None,
) -> SetupGrant | None:
    if settings.setup_token is not None:
        return None
    if AuthService(repository, session_ttl_seconds=settings.session_ttl_seconds).is_configured():
        return None
    return SetupGrant.generate(now=int(time.time()) if now is None else now)


def build_terminal_service(
    settings: WebSettings,
    repository: ControlPlaneRepository,
    bindings: list[Binding],
) -> TerminalService:
    targets = {binding.name: binding.tmux_target for binding in bindings}

    def resolve_target(session_id: str) -> str | None:
        managed = repository.get_managed_session(session_id)
        if managed is not None:
            return f"{managed.tmux_session}:{managed.tmux_window}.{managed.tmux_pane}"
        return targets.get(session_id)

    return TerminalService(
        repository=repository,
        target_resolver=resolve_target,
        allowed_origin=os.getenv("TMUXBOT_WEB_PUBLIC_ORIGIN") or f"http://{settings.host}:{settings.port}",
    )


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
