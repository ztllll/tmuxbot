from __future__ import annotations

import os
import secrets
import time

from fastapi import Cookie, Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi import status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from tmuxbot.control_plane.repository import ControlPlaneRepository
from tmuxbot.control_plane.tmux_inventory import TmuxInventory, classify_inventory
from tmuxbot.state import Binding
from tmuxbot.web.auth import AuthError, AuthenticatedSession, AuthService
from tmuxbot.web.schemas import PasswordRequest
from tmuxbot.web.settings import WebSettings


COOKIE_NAME = "tmuxbot_session"
BOOTSTRAP_COOKIE_NAME = "tmuxbot_bootstrap_csrf"
BOOTSTRAP_COOKIE_MAX_AGE = 300


def create_app(
    settings: WebSettings,
    repository: ControlPlaneRepository,
    inventory: TmuxInventory,
    bindings: list[Binding],
) -> FastAPI:
    app = FastAPI(
        title="tmuxbot control plane",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    auth = AuthService(repository, session_ttl_seconds=settings.session_ttl_seconds)
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
    def auth_status(response: Response) -> dict[str, bool | str]:
        csrf_token = auth.issue_bootstrap_token()
        set_bootstrap_cookie(response, csrf_token)
        return {"configured": auth.is_configured(), "csrf_token": csrf_token}

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

    @app.get("/api/tmux/sessions")
    def tmux_sessions(
        _: AuthenticatedSession = Depends(current_session),
    ) -> list[dict]:
        items = classify_inventory(
            inventory.list_panes(), bindings, ignored_targets=set()
        )
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

    return app
