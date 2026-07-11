from __future__ import annotations

import asyncio
import ipaddress
import os
import platform
import re
import secrets
import shutil
import socket
import subprocess
import time
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path
from contextlib import suppress
import tempfile

import yaml

from fastapi import (
    Cookie,
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    WebSocket,
)
from fastapi import status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from starlette.websockets import WebSocketDisconnect

from tmuxbot.control_plane.repository import ControlPlaneRepository
from tmuxbot.control_plane.models import ManagedSession, ProjectRecord, ProviderProfile
from tmuxbot.control_plane.tmux_inventory import (
    TmuxInventory,
    TmuxInventoryError,
    classify_inventory,
)
from tmuxbot.providers.discovery import ProviderDiscovery, ProviderDiscoveryError
from tmuxbot.paths import RuntimePaths
from tmuxbot.state import Binding
from tmuxbot.teamrun.domain import AgentRole, TeamRunSnapshot, TeamTask
from tmuxbot.teamrun.scheduler import ArtifactInput, TeamRunScheduler
from tmuxbot.web.auth import AuthError, AuthenticatedSession, AuthService
from tmuxbot.web.schemas import (
    ManagedSessionCreateRequest,
    PasswordRequest,
    ProjectCreateRequest,
    ChannelConfigureRequest,
    BlockTeamTaskRequest,
    CompleteTeamTaskRequest,
    CreateTeamRunRequest,
    IdempotentCommandRequest,
    ReviewTeamTaskRequest,
    StopTeamRunRequest,
)
from tmuxbot.web.setup import SetupGrant
from tmuxbot.web.settings import WebSettings
from tmuxbot.web.terminal import (
    TERMINAL_MAX_FRAME_BYTES,
    TerminalConnection,
    TerminalService,
    TerminalTicket,
    parse_resize_message,
)


COOKIE_NAME = "tmuxbot_session"
BOOTSTRAP_COOKIE_NAME = "tmuxbot_bootstrap_csrf"
BOOTSTRAP_COOKIE_MAX_AGE = 300
STATIC_DIR = Path(__file__).with_name("static")


def create_app(
    settings: WebSettings,
    repository: ControlPlaneRepository,
    inventory: TmuxInventory,
    bindings: list[Binding],
    *,
    setup_grant: SetupGrant | None = None,
    bridge_status: Callable[[], Mapping[str, object]] | None = None,
    provider_discovery: ProviderDiscovery | None = None,
    terminal_service: TerminalService | None = None,
    runtime_paths: RuntimePaths | None = None,
    teamrun_scheduler: TeamRunScheduler | None = None,
) -> FastAPI:
    app = FastAPI(
        title="tmuxbot control plane",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.mount(
        "/assets",
        StaticFiles(directory=STATIC_DIR / "assets", check_dir=False),
        name="web-assets",
    )
    auth = AuthService(repository, session_ttl_seconds=settings.session_ttl_seconds)
    app.state.setup_grant = setup_grant
    app.state.bridge_status = bridge_status
    app.state.provider_discovery = provider_discovery or ProviderDiscovery()
    app.state.terminal_service = terminal_service
    app.state.runtime_paths = runtime_paths
    app.state.teamrun_scheduler = teamrun_scheduler
    configured_origin = os.getenv("TMUXBOT_WEB_PUBLIC_ORIGIN")
    allowed_origin = configured_origin or f"http://{settings.host}:{settings.port}"

    @app.exception_handler(RequestValidationError)
    async def sanitized_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        if request.url.path in {"/api/auth/setup", "/api/auth/login"}:
            detail: str | list[dict] = "invalid request"
        else:
            detail = []
            for error in exc.errors():
                sanitized = dict(error)
                sanitized.pop("input", None)
                detail.append(jsonable_encoder(sanitized))
        return JSONResponse(status_code=422, content={"detail": detail})

    @app.middleware("http")
    async def enforce_origin(request: Request, call_next):
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            if origin is not None and origin.rstrip("/") != allowed_origin.rstrip("/"):
                return JSONResponse(status_code=403, content={"detail": "invalid origin"})
        if request.method == "POST" and request.url.path == "/api/auth/setup":
            client_host = request.client.host if request.client is not None else ""
            try:
                is_loopback = ipaddress.ip_address(client_host).is_loopback
            except ValueError:
                is_loopback = False
            if not is_loopback:
                return JSONResponse(
                    status_code=403,
                    content={"detail": "setup is only allowed from loopback"},
                )
            submitted_setup_token = request.headers.get("x-setup-token") or ""
            now = int(time.time())
            if settings.setup_token is not None:
                authorized = secrets.compare_digest(
                    submitted_setup_token, settings.setup_token
                )
            elif setup_grant is not None:
                authorized = setup_grant.authorize(
                    submitted_setup_token, now=now
                )
            else:
                return JSONResponse(
                    status_code=503,
                    content={"detail": "password setup is unavailable"},
                )
            if not authorized:
                return JSONResponse(
                    status_code=403,
                    content={"detail": "invalid setup authorization"},
                )
        return await call_next(request)

    def current_session(
        tmuxbot_session: str | None = Cookie(default=None, alias=COOKIE_NAME),
    ) -> AuthenticatedSession:
        if tmuxbot_session is None:
            raise HTTPException(status_code=401, detail="authentication required")
        try:
            return auth.authenticate(tmuxbot_session, now=int(time.time()))
        except AuthError as exc:
            raise HTTPException(
                status_code=401, detail="authentication required"
            ) from exc

    def csrf_session(
        session: AuthenticatedSession = Depends(current_session),
        csrf: str | None = Header(default=None, alias="X-CSRF-Token"),
    ) -> AuthenticatedSession:
        if csrf is None or not secrets.compare_digest(csrf, session.csrf_token):
            raise HTTPException(status_code=403, detail="invalid csrf token")
        return session

    def require_terminal_service() -> TerminalService:
        service = app.state.terminal_service
        if not isinstance(service, TerminalService):
            raise HTTPException(status_code=404, detail="terminal unavailable")
        return service

    def bootstrap_csrf(
        cookie_token: str | None = Cookie(
            default=None, alias=BOOTSTRAP_COOKIE_NAME
        ),
        header_token: str | None = Header(default=None, alias="X-CSRF-Token"),
    ) -> None:
        if (
            cookie_token is None
            or header_token is None
            or not secrets.compare_digest(cookie_token, header_token)
        ):
            raise HTTPException(status_code=403, detail="invalid csrf token")
        try:
            auth.validate_bootstrap_token(
                cookie_token, max_age_seconds=BOOTSTRAP_COOKIE_MAX_AGE
            )
        except AuthError as exc:
            raise HTTPException(status_code=403, detail="invalid csrf token") from exc

    def set_session_cookie(response: Response, token: str) -> None:
        response.set_cookie(
            COOKIE_NAME,
            token,
            httponly=True,
            secure=settings.secure_cookie,
            samesite="lax",
            max_age=settings.session_ttl_seconds,
            path="/",
        )

    def set_bootstrap_cookie(response: Response, token: str) -> None:
        response.set_cookie(
            BOOTSTRAP_COOKIE_NAME,
            token,
            httponly=True,
            secure=settings.secure_cookie,
            samesite="strict",
            max_age=BOOTSTRAP_COOKIE_MAX_AGE,
            path="/",
        )

    def delete_bootstrap_cookie(response: Response) -> None:
        response.delete_cookie(
            BOOTSTRAP_COOKIE_NAME,
            path="/",
            secure=settings.secure_cookie,
            httponly=True,
            samesite="strict",
        )

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/auth/status")
    def auth_status(response: Response) -> dict[str, bool | str | int | None]:
        csrf_token = auth.issue_bootstrap_token()
        set_bootstrap_cookie(response, csrf_token)
        configured = auth.is_configured()
        now = int(time.time())
        grant_available = (
            settings.setup_token is None
            and setup_grant is not None
            and setup_grant.is_available(now=now)
        )
        setup_available = not configured and (
            settings.setup_token is not None or grant_available
        )
        return {
            "configured": configured,
            "setup_available": setup_available,
            "setup_expires_at": (
                setup_grant.expires_at
                if setup_available and grant_available and setup_grant is not None
                else None
            ),
            "csrf_token": csrf_token,
        }

    @app.post("/api/auth/setup", status_code=status.HTTP_201_CREATED)
    def setup_password(
        body: PasswordRequest,
        response: Response,
        _: None = Depends(bootstrap_csrf),
    ) -> dict[str, str]:
        try:
            session = auth.setup(body.password, now=int(time.time()))
        except AuthError as exc:
            if auth.is_configured():
                raise HTTPException(
                    status_code=409, detail="password is already configured"
                ) from exc
            raise HTTPException(status_code=400, detail="password setup failed") from exc
        if settings.setup_token is None and setup_grant is not None:
            setup_grant.consume()
        delete_bootstrap_cookie(response)
        set_session_cookie(response, session.token)
        return {"csrf_token": session.csrf_token}

    @app.post("/api/auth/login")
    def login(
        body: PasswordRequest,
        response: Response,
        _: None = Depends(bootstrap_csrf),
    ) -> dict[str, str]:
        try:
            session = auth.login(body.password, now=int(time.time()))
        except AuthError as exc:
            raise HTTPException(status_code=401, detail="invalid credentials") from exc
        delete_bootstrap_cookie(response)
        set_session_cookie(response, session.token)
        return {"csrf_token": session.csrf_token}

    @app.post("/api/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
    def logout(
        response: Response,
        session: AuthenticatedSession = Depends(csrf_session),
    ) -> None:
        auth.logout(session.token)
        response.delete_cookie(COOKIE_NAME, path="/")

    @app.get("/api/auth/session")
    def auth_session(
        session: AuthenticatedSession = Depends(current_session),
    ) -> dict[str, str]:
        return {"csrf_token": session.csrf_token}

    @app.get("/api/events")
    def events(
        after: int = Query(default=0, ge=0),
        limit: int = Query(default=100, ge=1, le=500),
        _: AuthenticatedSession = Depends(current_session),
    ) -> list[dict]:
        return [
            {
                "sequence": event.sequence,
                "event_id": event.event_id,
                "event_type": event.event_type,
                "aggregate_type": event.aggregate_type,
                "aggregate_id": event.aggregate_id,
                "payload": dict(event.payload),
                "occurred_at": event.occurred_at.isoformat(),
            }
            for event in repository.list_events(after_sequence=after, limit=limit)
        ]

    def serialize_provider(profile: ProviderProfile) -> dict[str, object]:
        return {
            "id": profile.id,
            "binary_name": profile.binary_name,
            "executable_path": profile.executable_path,
            "version": profile.version,
            "device": profile.device,
            "inode": profile.inode,
            "mtime_ns": profile.mtime_ns,
            "discovered_at": profile.discovered_at,
        }

    @app.get("/api/providers")
    def providers(
        _: AuthenticatedSession = Depends(current_session),
    ) -> list[dict[str, object]]:
        return [
            serialize_provider(profile)
            for profile in repository.list_provider_profiles()
        ]

    @app.post("/api/providers/scan")
    def scan_providers(
        _: AuthenticatedSession = Depends(csrf_session),
    ) -> list[dict[str, object]]:
        stored = [
            repository.upsert_provider_profile(candidate)
            for candidate in app.state.provider_discovery.scan()
        ]
        return [serialize_provider(profile) for profile in stored]

    @app.post("/api/providers/{provider_id}/probe")
    def probe_provider(
        provider_id: str,
        _: AuthenticatedSession = Depends(csrf_session),
    ) -> dict[str, object]:
        profile = repository.get_provider_profile(provider_id)
        if profile is None:
            raise HTTPException(status_code=404, detail="provider not found")
        try:
            result = app.state.provider_discovery.probe(profile)
        except ProviderDiscoveryError as exc:
            if exc.code == "identity_changed":
                raise HTTPException(
                    status_code=409,
                    detail="provider executable changed; rescan required",
                ) from exc
            raise HTTPException(status_code=400, detail="provider probe rejected") from exc
        repository.record_probe_result(result)
        if result.success:
            repository.update_provider_version(profile.id, result.version)
        return {
            "id": result.id,
            "provider_id": result.provider_id,
            "success": result.success,
            "version": result.version,
            "error_code": result.error_code,
            "exit_code": result.exit_code,
            "duration_ms": result.duration_ms,
            "output_truncated": result.output_truncated,
            "observed_at": result.observed_at,
        }

    @app.get("/api/projects")
    def projects(
        _: AuthenticatedSession = Depends(current_session),
    ) -> list[dict[str, object]]:
        return [
            {
                "id": project.id,
                "name": project.name,
                "root_path": project.root_path,
                "created_at": project.created_at,
            }
            for project in repository.list_projects()
        ]

    @app.post("/api/projects", status_code=status.HTTP_201_CREATED)
    def create_project(
        body: ProjectCreateRequest,
        _: AuthenticatedSession = Depends(csrf_session),
    ) -> dict[str, object]:
        try:
            root = Path(body.root_path).expanduser().resolve(strict=True)
            info = root.stat()
        except OSError as exc:
            raise HTTPException(status_code=400, detail="project path is unavailable") from exc
        if not root.is_dir():
            raise HTTPException(status_code=400, detail="project path must be a directory")
        project = ProjectRecord(
            id=f"project-{uuid.uuid4().hex}",
            name=body.name.strip(),
            root_path=str(root),
            device=info.st_dev,
            inode=info.st_ino,
            mtime_ns=info.st_mtime_ns,
            created_at=int(time.time()),
        )
        try:
            repository.create_project(project)
        except Exception as exc:
            raise HTTPException(status_code=409, detail="project already exists") from exc
        return {
            "id": project.id,
            "name": project.name,
            "root_path": project.root_path,
            "created_at": project.created_at,
        }

    @app.get("/api/managed-sessions")
    def managed_sessions(
        _: AuthenticatedSession = Depends(current_session),
    ) -> list[dict[str, object]]:
        return [
            {
                "id": item.id,
                "project_id": item.project_id,
                "provider_id": item.provider_id,
                "name": item.name,
                "tmux_target": f"{item.tmux_session}:{item.tmux_window}.{item.tmux_pane}",
                "status": item.status,
            }
            for item in repository.list_managed_sessions()
        ]

    @app.post("/api/managed-sessions", status_code=status.HTTP_201_CREATED)
    def create_managed_session(
        body: ManagedSessionCreateRequest,
        _: AuthenticatedSession = Depends(csrf_session),
    ) -> dict[str, object]:
        project = repository.get_project(body.project_id)
        provider = repository.get_provider_profile(body.provider_id)
        if project is None or provider is None or provider.binary_name not in {"claude", "codex"}:
            raise HTTPException(status_code=404, detail="project or provider not found")
        try:
            ProviderDiscovery._verify_identity(provider)
        except ProviderDiscoveryError as exc:
            raise HTTPException(status_code=409, detail="provider identity changed") from exc
        try:
            current = Path(project.root_path).stat()
        except OSError as exc:
            raise HTTPException(status_code=409, detail="project identity changed") from exc
        if (current.st_dev, current.st_ino) != (project.device, project.inode):
            raise HTTPException(status_code=409, detail="project identity changed")
        tmux_binary = shutil.which("tmux")
        if tmux_binary is None:
            raise HTTPException(status_code=503, detail="tmux is unavailable")
        safe_provider = re.sub(r"[^a-z0-9]+", "-", provider.binary_name.lower()).strip("-")
        tmux_session = f"tmuxbot-{safe_provider}-{uuid.uuid4().hex[:8]}"
        provider_argv = [provider.executable_path]
        if provider.binary_name == "claude":
            provider_argv.append("--dangerously-skip-permissions")
        else:
            provider_argv.append("--dangerously-bypass-approvals-and-sandbox")
        # tmux accepts a command argv after its options. No browser-supplied command or target
        # enters this call; provider path and cwd come from server-verified records.
        completed = subprocess.run(
            [
                tmux_binary,
                "new-session",
                "-d",
                "-s",
                tmux_session,
                "-c",
                project.root_path,
                *provider_argv,
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if completed.returncode != 0:
            raise HTTPException(status_code=503, detail="unable to create tmux session")
        managed = ManagedSession(
            id=f"session-{uuid.uuid4().hex}",
            project_id=project.id,
            provider_id=provider.id,
            name=body.name.strip(),
            tmux_session=tmux_session,
            tmux_window=0,
            tmux_pane=0,
            status="running",
            created_at=int(time.time()),
        )
        repository.create_managed_session(managed)
        return {
            "id": managed.id,
            "name": managed.name,
            "provider": provider.binary_name,
            "tmux_target": f"{tmux_session}:0.0",
            "status": managed.status,
        }

    def require_runtime_paths() -> RuntimePaths:
        paths = app.state.runtime_paths
        if not isinstance(paths, RuntimePaths):
            raise HTTPException(status_code=503, detail="runtime configuration unavailable")
        return paths

    def write_private_text(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as handle:
            handle.write(content)
            temp_path = Path(handle.name)
        os.chmod(temp_path, 0o600)
        os.replace(temp_path, path)

    @app.get("/api/channels")
    def channels(
        _: AuthenticatedSession = Depends(current_session),
        paths: RuntimePaths = Depends(require_runtime_paths),
    ) -> list[dict[str, object]]:
        env_text = paths.env_file.read_text(encoding="utf-8") if paths.env_file.is_file() else ""
        return [
            {"channel": "telegram", "configured": "TG_BOT_TOKEN=" in env_text or "TG_CODEX_BOT_TOKEN=" in env_text},
            {"channel": "feishu", "configured": "FEISHU_APP_ID=" in env_text and "FEISHU_APP_SECRET=" in env_text},
        ]

    @app.post("/api/channels/configure", status_code=status.HTTP_201_CREATED)
    def configure_channel(
        body: ChannelConfigureRequest,
        _: AuthenticatedSession = Depends(csrf_session),
        paths: RuntimePaths = Depends(require_runtime_paths),
    ) -> dict[str, object]:
        managed = repository.get_managed_session(body.managed_session_id)
        if managed is None:
            raise HTTPException(status_code=404, detail="managed session not found")
        provider = repository.get_provider_profile(managed.provider_id)
        project = repository.get_project(managed.project_id)
        if provider is None or project is None:
            raise HTTPException(status_code=409, detail="managed session is incomplete")
        values = (body.credential_id, body.credential_secret or "", body.boss_id, body.remote_chat_id)
        if any("\n" in value or "\r" in value or "\0" in value for value in values):
            raise HTTPException(status_code=400, detail="invalid channel value")
        env_values: dict[str, str] = {}
        if body.channel == "telegram":
            if ":" not in body.credential_id or not body.boss_id.lstrip("-").isdigit():
                raise HTTPException(status_code=400, detail="invalid Telegram credentials")
            token_env = "TG_BOT_TOKEN" if provider.binary_name == "claude" else "TG_CODEX_BOT_TOKEN"
            env_values[token_env] = body.credential_id
            env_values["BOSS_USER_ID"] = body.boss_id
            chat_id: int | str = int(body.remote_chat_id) if body.remote_chat_id.lstrip("-").isdigit() else body.remote_chat_id
        else:
            if not body.credential_secret:
                raise HTTPException(status_code=400, detail="Feishu secret is required")
            token_env = "FEISHU"
            env_values.update({
                "FEISHU_APP_ID": body.credential_id,
                "FEISHU_APP_SECRET": body.credential_secret,
                "FEISHU_BOSS_OPEN_IDS": body.boss_id,
            })
            chat_id = body.remote_chat_id
        existing_lines = paths.env_file.read_text(encoding="utf-8").splitlines() if paths.env_file.is_file() else []
        retained = [line for line in existing_lines if line.split("=", 1)[0].strip() not in env_values]
        rendered_env = "\n".join([*retained, *(f"{key}={value}" for key, value in env_values.items())]).strip() + "\n"
        raw = yaml.safe_load(paths.bindings_file.read_text(encoding="utf-8")) if paths.bindings_file.is_file() else {}
        if not isinstance(raw, dict):
            raw = {}
        entries = raw.get("bindings")
        if not isinstance(entries, list):
            entries = []
        binding_name = f"web-{body.channel}-{managed.id}"
        entry = {
            "name": binding_name,
            "channel": body.channel,
            "chat_id": chat_id,
            "thread_id": None,
            "bot_token_env": token_env,
            "backend": "claude_code" if provider.binary_name == "claude" else "codex",
            "tmux_session": managed.tmux_session,
            "tmux_window": managed.tmux_window,
            "tmux_pane": managed.tmux_pane,
            "cwd": project.root_path,
            "mention_required": body.mention_required,
        }
        entries = [item for item in entries if not isinstance(item, dict) or item.get("name") != binding_name]
        entries.append(entry)
        raw["bindings"] = entries
        write_private_text(paths.env_file, rendered_env)
        write_private_text(paths.bindings_file, yaml.safe_dump(raw, allow_unicode=True, sort_keys=False))
        bridge_snapshot = app.state.bridge_status() if callable(app.state.bridge_status) else {}
        restart_required = bridge_snapshot.get("state") == "running"
        return {
            "channel": body.channel,
            "configured": True,
            "binding_name": binding_name,
            "restart_required": restart_required,
        }

    @app.get("/api/system/status")
    def system_status(
        _: AuthenticatedSession = Depends(current_session),
    ) -> dict[str, object]:
        bridge_callback = app.state.bridge_status
        bridge = (
            dict(bridge_callback())
            if callable(bridge_callback)
            else {"state": "standalone", "reason": "web_only"}
        )
        bridge.setdefault("status", bridge.get("state", "unknown"))
        provider_items = []
        for provider_name, binary_name in (("Claude Code", "claude"), ("Codex", "codex")):
            binary_path = shutil.which(binary_name)
            provider_items.append(
                {
                    "name": provider_name,
                    "status": "found" if binary_path else "missing",
                    "path": binary_path,
                }
            )
        tmux_path = shutil.which("tmux")
        return {
            "host": {
                "hostname": socket.gethostname(),
                "platform": platform.system(),
                "python_version": platform.python_version(),
            },
            "bridge": bridge,
            "tmux": {"status": "found" if tmux_path else "missing", "path": tmux_path},
            "paths": {"database": str(settings.database_path)},
            "providers": provider_items,
            "binding_count": len(bindings),
        }

    @app.get("/api/tmux/sessions")
    def tmux_sessions(
        _: AuthenticatedSession = Depends(current_session),
    ) -> list[dict]:
        try:
            panes = inventory.list_panes()
        except TmuxInventoryError as exc:
            raise HTTPException(
                status_code=503, detail="tmux inventory unavailable"
            ) from exc
        items = classify_inventory(panes, bindings, ignored_targets=set())
        return [
            {
                "target": item.pane.target,
                "session_name": item.pane.session_name,
                "window_index": item.pane.window_index,
                "pane_index": item.pane.pane_index,
                "command": item.pane.command,
                "cwd": item.pane.cwd,
                "pid": item.pane.pid,
                "classification": item.classification.value,
                "binding_name": item.binding_name,
                "provider": item.provider,
            }
            for item in items
        ]

    @app.post(
        "/api/terminals/{managed_session_id}/ticket",
        status_code=status.HTTP_201_CREATED,
    )
    def terminal_ticket(
        managed_session_id: str,
        session: AuthenticatedSession = Depends(csrf_session),
        service: TerminalService = Depends(require_terminal_service),
    ) -> dict[str, str | int]:
        ticket = service.issue_ticket(
            managed_session_id, session.token, now=int(time.time())
        )
        if ticket is None:
            raise HTTPException(status_code=404, detail="managed session not found")
        return {"ticket": ticket.token, "expires_at": ticket.expires_at}

    @app.post("/api/terminals/{managed_session_id}/takeover")
    def terminal_takeover(
        managed_session_id: str,
        session: AuthenticatedSession = Depends(csrf_session),
        service: TerminalService = Depends(require_terminal_service),
    ) -> dict[str, str]:
        outcome = service.start_takeover(managed_session_id, session.token)
        if outcome == "missing":
            raise HTTPException(status_code=404, detail="managed session not found")
        if outcome in {"conflict", "not_connected"}:
            raise HTTPException(status_code=409, detail="terminal already controlled")
        return {"mode": "takeover"}

    @app.delete("/api/terminals/{managed_session_id}/takeover")
    def terminal_release(
        managed_session_id: str,
        session: AuthenticatedSession = Depends(csrf_session),
        service: TerminalService = Depends(require_terminal_service),
    ) -> dict[str, str]:
        if not service.end_takeover(
            managed_session_id, session.token, reason="api"
        ):
            raise HTTPException(status_code=409, detail="terminal is not controlled")
        return {"mode": "observe"}

    async def reject_terminal(websocket: WebSocket, code: int) -> None:
        await websocket.accept()
        await websocket.close(code=code)

    async def terminal_output_loop(
        websocket: WebSocket, terminal: TerminalConnection
    ) -> None:
        while True:
            data = await terminal.read(TERMINAL_MAX_FRAME_BYTES)
            if not data:
                return
            await websocket.send_bytes(data)

    async def terminal_input_loop(
        websocket: WebSocket,
        terminal: TerminalConnection,
        ticket: TerminalTicket,
        session_token: str,
        service: TerminalService,
    ) -> None:
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                return
            data = message.get("bytes")
            if data is not None:
                if len(data) > TERMINAL_MAX_FRAME_BYTES:
                    await websocket.close(code=1009)
                    return
                if not service.can_input(ticket.managed_session_id, session_token):
                    await websocket.send_json(
                        {"type": "input_rejected", "reason": "observe_only"}
                    )
                    continue
                await terminal.write(data)
                continue
            text = message.get("text")
            if text is None or len(text.encode("utf-8")) > TERMINAL_MAX_FRAME_BYTES:
                await websocket.send_json(
                    {"type": "message_rejected", "reason": "invalid_frame"}
                )
                continue
            resize = parse_resize_message(text)
            if resize is None:
                await websocket.send_json(
                    {"type": "message_rejected", "reason": "invalid_frame"}
                )
                continue
            rows, cols = resize
            await terminal.resize(rows, cols)

    @app.websocket("/api/terminals/ws")
    async def terminal_websocket(websocket: WebSocket, ticket: str = Query(...)) -> None:
        service = app.state.terminal_service
        if not isinstance(service, TerminalService):
            await reject_terminal(websocket, 4404)
            return
        origin = websocket.headers.get("origin")
        if origin is None or origin.rstrip("/") != service.allowed_origin:
            await reject_terminal(websocket, 4403)
            return
        session_token = websocket.cookies.get(COOKIE_NAME)
        if session_token is None:
            await reject_terminal(websocket, 4401)
            return
        try:
            auth.authenticate(session_token, now=int(time.time()))
        except AuthError:
            await reject_terminal(websocket, 4401)
            return
        terminal_ticket = service.consume_ticket(
            ticket, session_token, now=int(time.time())
        )
        if terminal_ticket is None:
            await reject_terminal(websocket, 4403)
            return
        try:
            terminal = await service.open_terminal(terminal_ticket)
        except Exception:
            await reject_terminal(websocket, 1011)
            return
        await websocket.accept()
        service.connect(terminal_ticket.managed_session_id, session_token)
        output_task = asyncio.create_task(terminal_output_loop(websocket, terminal))
        input_task = asyncio.create_task(
            terminal_input_loop(
                websocket,
                terminal,
                terminal_ticket,
                session_token,
                service,
            )
        )
        try:
            done, pending = await asyncio.wait(
                {output_task, input_task}, return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
            for task in done | pending:
                with suppress(asyncio.CancelledError, WebSocketDisconnect):
                    await task
        finally:
            try:
                service.end_takeover(
                    terminal_ticket.managed_session_id,
                    session_token,
                    reason="disconnect",
                )
            finally:
                try:
                    service.disconnect(
                        terminal_ticket.managed_session_id, session_token
                    )
                finally:
                    await terminal.close()

    def scheduler_service() -> TeamRunScheduler:
        scheduler = app.state.teamrun_scheduler
        if not isinstance(scheduler, TeamRunScheduler):
            raise HTTPException(status_code=503, detail="teamrun scheduler unavailable")
        return scheduler

    def scheduler_call(operation):
        try:
            return operation()
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="team run or task not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/team-runs", status_code=status.HTTP_201_CREATED)
    def create_team_run(
        body: CreateTeamRunRequest,
        _: AuthenticatedSession = Depends(csrf_session),
    ) -> dict[str, object]:
        roles = {AgentRole(item.role): item.managed_session_id for item in body.agents}
        if len(roles) != len(body.agents):
            raise HTTPException(status_code=409, detail="agent roles must be unique")
        snapshot = scheduler_call(
            lambda: scheduler_service().create_deterministic_run(
                run_id=body.run_id,
                goal=body.goal,
                agents=roles,
                tasks=[item.model_dump() for item in body.tasks],
                idempotency_key=body.idempotency_key,
            )
        )
        return _serialize_teamrun(snapshot)

    @app.get("/api/team-runs/{run_id}")
    def get_team_run(
        run_id: str,
        _: AuthenticatedSession = Depends(current_session),
    ) -> dict[str, object]:
        snapshot = scheduler_call(
            lambda: scheduler_service().repository.get_team_run(run_id)
        )
        return _serialize_teamrun(snapshot)

    @app.get("/api/team-runs/{run_id}/mailbox")
    def get_team_run_mailbox(
        run_id: str,
        _: AuthenticatedSession = Depends(current_session),
    ) -> list[dict[str, object]]:
        scheduler_call(lambda: scheduler_service().repository.get_team_run(run_id))
        return [
            {
                "message_id": message.message_id,
                "task_id": message.task_id,
                "sender_agent_id": message.sender_agent_id,
                "recipient_agent_id": message.recipient_agent_id,
                "kind": message.kind,
                "body": dict(message.body),
                "created_at": message.created_at.isoformat(),
                "delivered_at": (
                    message.delivered_at.isoformat() if message.delivered_at else None
                ),
            }
            for message in scheduler_service().repository.list_mailbox(run_id)
        ]

    @app.get("/api/team-runs/{run_id}/artifacts")
    def get_team_run_artifacts(
        run_id: str,
        _: AuthenticatedSession = Depends(current_session),
    ) -> list[dict[str, object]]:
        scheduler_call(lambda: scheduler_service().repository.get_team_run(run_id))
        return [
            {
                "artifact_id": artifact.artifact_id,
                "task_id": artifact.task_id,
                "producer_agent_id": artifact.producer_agent_id,
                "kind": artifact.kind,
                "uri": artifact.uri,
                "metadata": dict(artifact.metadata),
                "created_at": artifact.created_at.isoformat(),
            }
            for artifact in scheduler_service().repository.list_artifacts(run_id)
        ]

    @app.post("/api/team-runs/{run_id}/start")
    def start_team_run(
        run_id: str,
        body: IdempotentCommandRequest,
        _: AuthenticatedSession = Depends(csrf_session),
    ) -> dict[str, object]:
        return _serialize_teamrun(
            scheduler_call(
                lambda: scheduler_service().start(
                    run_id, idempotency_key=body.idempotency_key
                )
            )
        )

    @app.post("/api/team-runs/{run_id}/pause")
    def pause_team_run(
        run_id: str,
        body: IdempotentCommandRequest,
        _: AuthenticatedSession = Depends(csrf_session),
    ) -> dict[str, object]:
        return _serialize_teamrun(
            scheduler_call(
                lambda: scheduler_service().pause(
                    run_id, idempotency_key=body.idempotency_key
                )
            )
        )

    @app.post("/api/team-runs/{run_id}/resume")
    def resume_team_run(
        run_id: str,
        body: IdempotentCommandRequest,
        _: AuthenticatedSession = Depends(csrf_session),
    ) -> dict[str, object]:
        return _serialize_teamrun(
            scheduler_call(
                lambda: scheduler_service().resume(
                    run_id, idempotency_key=body.idempotency_key
                )
            )
        )

    @app.post("/api/team-runs/{run_id}/stop")
    def stop_team_run(
        run_id: str,
        body: StopTeamRunRequest,
        _: AuthenticatedSession = Depends(csrf_session),
    ) -> dict[str, object]:
        return _serialize_teamrun(
            scheduler_call(
                lambda: scheduler_service().stop(
                    run_id,
                    reason=body.reason,
                    idempotency_key=body.idempotency_key,
                )
            )
        )

    @app.post("/api/team-runs/{run_id}/tasks/{task_id}/complete")
    def complete_team_task(
        run_id: str,
        task_id: str,
        body: CompleteTeamTaskRequest,
        _: AuthenticatedSession = Depends(csrf_session),
    ) -> dict[str, object]:
        task = scheduler_call(
            lambda: scheduler_service().complete_task(
                run_id,
                task_id,
                agent_id=body.agent_id,
                artifacts=[
                    ArtifactInput(item.kind, item.uri, item.metadata)
                    for item in body.artifacts
                ],
                idempotency_key=body.idempotency_key,
            )
        )
        return _serialize_team_task(task)

    @app.post("/api/team-runs/{run_id}/tasks/{task_id}/review")
    def review_team_task(
        run_id: str,
        task_id: str,
        body: ReviewTeamTaskRequest,
        _: AuthenticatedSession = Depends(csrf_session),
    ) -> dict[str, object]:
        task = scheduler_call(
            lambda: scheduler_service().review_task(
                run_id,
                task_id,
                reviewer_agent_id=body.reviewer_agent_id,
                verdict=body.verdict,
                notes=body.notes,
                idempotency_key=body.idempotency_key,
            )
        )
        return _serialize_team_task(task)

    @app.post("/api/team-runs/{run_id}/tasks/{task_id}/blocked")
    def block_team_task(
        run_id: str,
        task_id: str,
        body: BlockTeamTaskRequest,
        _: AuthenticatedSession = Depends(csrf_session),
    ) -> dict[str, object]:
        task = scheduler_call(
            lambda: scheduler_service().block_task(
                run_id,
                task_id,
                agent_id=body.agent_id,
                reason=body.reason,
                idempotency_key=body.idempotency_key,
            )
        )
        return _serialize_team_task(task)

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa_fallback(full_path: str):
        if full_path in {"openapi.json", "docs", "redoc"} or full_path.startswith(
            ("api/", "assets/", "docs/", "redoc/")
        ):
            raise HTTPException(status_code=404, detail="not found")
        index_file = STATIC_DIR / "index.html"
        if index_file.is_file():
            return FileResponse(index_file)
        return PlainTextResponse(
            "WebUI 尚未构建。请在源码目录运行 `cd webui && npm run build`，"
            "或重新安装包含静态资源的 tmuxbot wheel。API 仍保持可用。",
            status_code=503,
        )

    return app


def _serialize_team_task(task: TeamTask) -> dict[str, object]:
    return {
        "task_id": task.task_id,
        "run_id": task.run_id,
        "title": task.title,
        "goal": task.goal,
        "role": task.role.value,
        "state": task.state.value,
        "dependencies": list(task.dependencies),
        "requires_write": task.requires_write,
        "max_attempts": task.max_attempts,
        "attempt": task.attempt,
        "assignee_agent_id": task.assignee_agent_id,
    }


def _serialize_teamrun(snapshot: TeamRunSnapshot) -> dict[str, object]:
    return {
        "run": {
            "run_id": snapshot.run.run_id,
            "goal": snapshot.run.goal,
            "state": snapshot.run.state.value,
            "max_retries": snapshot.run.max_retries,
        },
        "agents": [
            {
                "agent_id": agent.agent_id,
                "role": agent.role.value,
                "managed_session_id": agent.managed_session_id,
            }
            for agent in snapshot.agents
        ],
        "tasks": [_serialize_team_task(task) for task in snapshot.tasks],
    }
