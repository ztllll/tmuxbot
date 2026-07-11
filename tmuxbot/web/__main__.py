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
from tmuxbot.state import Binding, S
from tmuxbot.web.app import create_app
from tmuxbot.web.auth import AuthService
from tmuxbot.web.setup import SetupGrant
from tmuxbot.web.settings import WebSettings
from tmuxbot.web.terminal import TerminalService
from tmuxbot.teamrun.scheduler import TeamRunScheduler
from tmuxbot.teamrun.tmux_sender import TmuxManagedSender


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


def build_terminal_service(
    settings: WebSettings,
    repository: ControlPlaneRepository,
    bindings: list[Binding],
) -> TerminalService:
    targets = {binding.name: binding.tmux_target for binding in bindings}

    def resolve_target(managed_session_id: str) -> str | None:
        managed = repository.get_managed_session(managed_session_id)
        if managed is not None:
            return f"{managed.tmux_session}:{managed.tmux_window}.{managed.tmux_pane}"
        return targets.get(managed_session_id)

    configured_origin = os.getenv("TMUXBOT_WEB_PUBLIC_ORIGIN")
    allowed_origin = configured_origin or f"http://{settings.host}:{settings.port}"
    return TerminalService(
        repository=repository,
        target_resolver=resolve_target,
        allowed_origin=allowed_origin,
    )


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
    options = {}
    if setup_grant is not None:
        options["setup_grant"] = setup_grant
    if isinstance(settings, WebSettings):
        options["terminal_service"] = build_terminal_service(
            settings, repository, S.bindings
        )
        options["runtime_paths"] = paths
        scheduler = TeamRunScheduler(repository, TmuxManagedSender(repository))
        scheduler.reconcile()
        options["teamrun_scheduler"] = scheduler
    app = create_app(
        settings,
        repository,
        TmuxInventory(),
        S.bindings,
        **options,
    )
    return settings, app


def run_web() -> None:
    settings, app = build_app()
    uvicorn.run(app, host=settings.host, port=settings.port, proxy_headers=False)


if __name__ == "__main__":
    run_web()
