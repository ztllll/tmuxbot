# WebUI 控制面基础实施计划

> **Agent 执行要求：** 必须使用 `superpowers:subagent-driven-development`（推荐）或 `superpowers:executing-plans` 逐任务实施。所有步骤使用复选框跟踪。

**目标：** 交付可独立启动、默认安全、可持久化的 tmuxbot Web 控制面基础，包括领域契约、SQLite、追加式事件、单用户认证和只读 tmux 清单。

**架构：** 新增 `tmuxbot/control_plane/` 保存通道无关的模型、迁移、Repository、事件与 tmux inventory；新增 `tmuxbot/web/` 负责认证和 FastAPI 装配。现有 Telegram/飞书进程不导入或启动 Web 服务，`tmuxbot web` 作为单独进程运行。

**技术栈：** Python 3.10+、FastAPI、Uvicorn、SQLite、pwdlib/Argon2、itsdangerous、pytest、HTTPX。

## 全局约束

- tmux 是唯一执行面；本阶段所有 tmux 操作严格只读。
- 默认监听 `127.0.0.1:8765`；非本机监听必须显式设置 `TMUXBOT_WEB_HOST`。
- V1 单用户；除健康检查、认证状态、首次设置和登录外，所有 API 必须认证。
- Cookie 必须为 HTTP-only、SameSite=Lax；生产模式要求 Secure。
- 所有写 API 必须验证 `X-CSRF-Token`。
- 密码使用 Argon2；数据库只保存密码 hash 和 session token hash。
- `RunEvent` 先持久化再投影，`event_id` 全局唯一并支持幂等追加。
- 不修改现有 Telegram、飞书、binding、tmux target 或 provider session identity 行为。

---

### Task 1：Web 依赖与配置契约

**Files:**
- Modify: `pyproject.toml`
- Create: `tmuxbot/web/__init__.py`
- Create: `tmuxbot/web/settings.py`
- Test: `tests/web/test_web_settings.py`

**Interfaces:**
- Consumes: 环境变量和现有 `TMUXBOT_DATA_DIR` 约定。
- Produces: `WebSettings.from_env() -> WebSettings`；后续任务统一依赖该不可变配置对象。

- [ ] **Step 1：写失败测试**

```python
from pathlib import Path

from tmuxbot.web.settings import WebSettings


def test_web_settings_are_local_and_secure_by_default(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("TMUXBOT_DATA_DIR", str(tmp_path))
    for name in ("TMUXBOT_WEB_HOST", "TMUXBOT_WEB_PORT", "TMUXBOT_WEB_SECURE_COOKIE"):
        monkeypatch.delenv(name, raising=False)

    settings = WebSettings.from_env()

    assert settings.host == "127.0.0.1"
    assert settings.port == 8765
    assert settings.database_path == tmp_path / "control-plane.sqlite3"
    assert settings.secure_cookie is False


def test_web_settings_parse_explicit_remote_deployment(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("TMUXBOT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TMUXBOT_WEB_HOST", "0.0.0.0")
    monkeypatch.setenv("TMUXBOT_WEB_PORT", "9443")
    monkeypatch.setenv("TMUXBOT_WEB_SECURE_COOKIE", "true")

    settings = WebSettings.from_env()

    assert settings.host == "0.0.0.0"
    assert settings.port == 9443
    assert settings.secure_cookie is True
```

- [ ] **Step 2：运行测试并确认失败**

Run: `uv run pytest tests/web/test_web_settings.py -v`

Expected: FAIL，提示 `tmuxbot.web.settings` 不存在。

- [ ] **Step 3：增加依赖和最小实现**

在 `pyproject.toml` 增加：

```toml
[project.optional-dependencies]
web = [
    "fastapi>=0.116,<1",
    "uvicorn[standard]>=0.35,<1",
    "pwdlib[argon2]>=0.2,<1",
    "itsdangerous>=2.2,<3",
]
dev = [
    "pytest>=8.0",
    "ruff>=0.6",
    "httpx>=0.28,<1",
    "tomli>=2.0; python_version < '3.11'",
]
```

创建 `tmuxbot/web/settings.py`：

```python
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
```

创建空的 `tmuxbot/web/__init__.py`。

- [ ] **Step 4：运行测试并确认通过**

Run: `uv sync --extra dev --extra web && uv run pytest tests/web/test_web_settings.py -v`

Expected: 2 passed。

- [ ] **Step 5：提交**

```bash
git add pyproject.toml uv.lock tmuxbot/web tests/web/test_web_settings.py
git commit -m "feat(web): add secure web settings"
```

### Task 2：领域模型与状态约束

**Files:**
- Create: `tmuxbot/control_plane/__init__.py`
- Create: `tmuxbot/control_plane/models.py`
- Test: `tests/control_plane/test_models.py`

**Interfaces:**
- Consumes: Python 标准库。
- Produces: `RunState`、`TaskState`、`SessionClass`、`RunEvent`、`TmuxPaneRecord`、`SessionInventoryItem`。

- [ ] **Step 1：写失败测试**

```python
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone

import pytest

from tmuxbot.control_plane.models import RunEvent, SessionClass, TaskState


def test_run_event_is_immutable_and_uses_utc_timestamp():
    event = RunEvent(
        event_id="evt-1",
        event_type="session.discovered",
        aggregate_type="session",
        aggregate_id="alpha:0.0",
        payload={"classification": "managed"},
        occurred_at=datetime.now(timezone.utc),
    )

    assert event.occurred_at.tzinfo is timezone.utc
    with pytest.raises(FrozenInstanceError):
        event.event_type = "changed"  # type: ignore[misc]


def test_foundation_enums_keep_storage_values_stable():
    assert TaskState.OPERATOR_REQUIRED.value == "operator_required"
    assert SessionClass.ORPHAN.value == "orphan"
```

- [ ] **Step 2：运行测试并确认失败**

Run: `uv run pytest tests/control_plane/test_models.py -v`

Expected: FAIL，提示 `tmuxbot.control_plane.models` 不存在。

- [ ] **Step 3：实现不可变领域契约**

创建 `tmuxbot/control_plane/models.py`：

```python
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping


class RunState(str, Enum):
    DRAFT = "draft"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    OPERATOR_REQUIRED = "operator_required"


class TaskState(str, Enum):
    PENDING = "pending"
    READY = "ready"
    ASSIGNED = "assigned"
    WORKING = "working"
    REVIEW = "review"
    ACCEPTED = "accepted"
    BLOCKED = "blocked"
    FAILED = "failed"
    RETRYING = "retrying"
    OPERATOR_REQUIRED = "operator_required"


class SessionClass(str, Enum):
    MANAGED = "managed"
    ORPHAN = "orphan"
    IGNORED = "ignored"


@dataclass(frozen=True, slots=True)
class RunEvent:
    event_id: str
    event_type: str
    aggregate_type: str
    aggregate_id: str
    payload: Mapping[str, Any]
    occurred_at: datetime
    sequence: int | None = None

    def __post_init__(self) -> None:
        if self.occurred_at.tzinfo is None:
            raise ValueError("occurred_at must be timezone-aware")
        object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))


@dataclass(frozen=True, slots=True)
class TmuxPaneRecord:
    target: str
    session_name: str
    window_index: int
    pane_index: int
    command: str
    cwd: str
    pid: int


@dataclass(frozen=True, slots=True)
class SessionInventoryItem:
    pane: TmuxPaneRecord
    classification: SessionClass
    binding_name: str | None = None
    provider: str | None = None
    observed_at: datetime | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))
```

创建空的 `tmuxbot/control_plane/__init__.py`。

- [ ] **Step 4：运行测试并确认通过**

Run: `uv run pytest tests/control_plane/test_models.py -v`

Expected: 2 passed。

- [ ] **Step 5：提交**

```bash
git add tmuxbot/control_plane tests/control_plane/test_models.py
git commit -m "feat(control-plane): add domain contracts"
```

### Task 3：SQLite migration 与 Repository

**Files:**
- Create: `tmuxbot/control_plane/migrations.py`
- Create: `tmuxbot/control_plane/repository.py`
- Test: `tests/control_plane/test_repository.py`

**Interfaces:**
- Consumes: `RunEvent`。
- Produces: `ControlPlaneRepository.migrate()`、`append_event()`、`list_events()`、`get_setting()`、`set_setting()`、`create_session()`、`get_session()`、`delete_session()`。

- [ ] **Step 1：写失败测试**

```python
from datetime import datetime, timezone

from tmuxbot.control_plane.models import RunEvent
from tmuxbot.control_plane.repository import ControlPlaneRepository


def test_repository_migrates_and_appends_event_idempotently(tmp_path):
    repo = ControlPlaneRepository(tmp_path / "control.sqlite3")
    repo.migrate()
    event = RunEvent(
        event_id="evt-1",
        event_type="session.discovered",
        aggregate_type="session",
        aggregate_id="alpha:0.0",
        payload={"classification": "orphan"},
        occurred_at=datetime(2026, 7, 11, tzinfo=timezone.utc),
    )

    assert repo.append_event(event) is True
    assert repo.append_event(event) is False
    stored = repo.list_events(after_sequence=0, limit=10)
    assert len(stored) == 1
    assert stored[0].sequence == 1
    assert stored[0].payload == {"classification": "orphan"}


def test_repository_persists_settings_and_web_sessions(tmp_path):
    repo = ControlPlaneRepository(tmp_path / "control.sqlite3")
    repo.migrate()
    repo.set_setting("auth.password_hash", "argon2-value")
    repo.create_session("token-hash", "csrf-value", expires_at=2_000_000_000)

    assert repo.get_setting("auth.password_hash") == "argon2-value"
    assert repo.get_session("token-hash", now=1_900_000_000) == "csrf-value"
    assert repo.get_session("token-hash", now=2_100_000_000) is None
```

- [ ] **Step 2：运行测试并确认失败**

Run: `uv run pytest tests/control_plane/test_repository.py -v`

Expected: FAIL，提示 Repository 不存在。

- [ ] **Step 3：增加编号 migration**

创建 `tmuxbot/control_plane/migrations.py`：

```python
MIGRATIONS: tuple[tuple[int, str], ...] = (
    (
        1,
        """
        CREATE TABLE settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at INTEGER NOT NULL
        );
        CREATE TABLE web_sessions (
            token_hash TEXT PRIMARY KEY,
            csrf_token TEXT NOT NULL,
            expires_at INTEGER NOT NULL,
            created_at INTEGER NOT NULL
        );
        CREATE TABLE run_events (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT NOT NULL UNIQUE,
            event_type TEXT NOT NULL,
            aggregate_type TEXT NOT NULL,
            aggregate_id TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            occurred_at TEXT NOT NULL
        );
        CREATE INDEX run_events_aggregate_idx
            ON run_events(aggregate_type, aggregate_id, sequence);
        """,
    ),
)
```

- [ ] **Step 4：实现 Repository**

创建 `tmuxbot/control_plane/repository.py`，实现以下完整公开行为：

```python
from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime
from pathlib import Path

from tmuxbot.control_plane.migrations import MIGRATIONS
from tmuxbot.control_plane.models import RunEvent


class ControlPlaneRepository:
    def __init__(self, path: Path):
        self.path = path

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def migrate(self) -> None:
        with self._connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations "
                "(version INTEGER PRIMARY KEY, applied_at INTEGER NOT NULL)"
            )
            applied = {row[0] for row in db.execute("SELECT version FROM schema_migrations")}
            for version, sql in MIGRATIONS:
                if version in applied:
                    continue
                db.executescript(sql)
                db.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (version, int(time.time())),
                )

    def append_event(self, event: RunEvent) -> bool:
        with self._connect() as db:
            cursor = db.execute(
                "INSERT OR IGNORE INTO run_events "
                "(event_id, event_type, aggregate_type, aggregate_id, payload_json, occurred_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    event.event_id,
                    event.event_type,
                    event.aggregate_type,
                    event.aggregate_id,
                    json.dumps(dict(event.payload), ensure_ascii=False, sort_keys=True),
                    event.occurred_at.isoformat(),
                ),
            )
            return cursor.rowcount == 1

    def list_events(self, *, after_sequence: int, limit: int) -> list[RunEvent]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM run_events WHERE sequence > ? ORDER BY sequence LIMIT ?",
                (after_sequence, min(max(limit, 1), 500)),
            ).fetchall()
        return [
            RunEvent(
                event_id=row["event_id"],
                event_type=row["event_type"],
                aggregate_type=row["aggregate_type"],
                aggregate_id=row["aggregate_id"],
                payload=json.loads(row["payload_json"]),
                occurred_at=datetime.fromisoformat(row["occurred_at"]),
                sequence=row["sequence"],
            )
            for row in rows
        ]

    def set_setting(self, key: str, value: str) -> None:
        with self._connect() as db:
            db.execute(
                "INSERT INTO settings(key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (key, value, int(time.time())),
            )

    def get_setting(self, key: str) -> str | None:
        with self._connect() as db:
            row = db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return None if row is None else str(row["value"])

    def create_session(self, token_hash: str, csrf_token: str, *, expires_at: int) -> None:
        with self._connect() as db:
            now = int(time.time())
            db.execute("DELETE FROM web_sessions WHERE expires_at <= ?", (now,))
            db.execute(
                "INSERT INTO web_sessions(token_hash, csrf_token, expires_at, created_at) "
                "VALUES (?, ?, ?, ?)",
                (token_hash, csrf_token, expires_at, now),
            )

    def get_session(self, token_hash: str, *, now: int) -> str | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT csrf_token FROM web_sessions "
                "WHERE token_hash = ? AND expires_at > ?",
                (token_hash, now),
            ).fetchone()
        return None if row is None else str(row["csrf_token"])

    def delete_session(self, token_hash: str) -> None:
        with self._connect() as db:
            db.execute("DELETE FROM web_sessions WHERE token_hash = ?", (token_hash,))
```

- [ ] **Step 5：运行测试并确认通过**

Run: `uv run pytest tests/control_plane/test_repository.py -v`

Expected: 2 passed。

- [ ] **Step 6：提交**

```bash
git add tmuxbot/control_plane tests/control_plane/test_repository.py
git commit -m "feat(control-plane): add sqlite event repository"
```

### Task 4：单用户密码、Session Cookie 与 CSRF

**Files:**
- Create: `tmuxbot/web/auth.py`
- Test: `tests/web/test_auth_service.py`

**Interfaces:**
- Consumes: `ControlPlaneRepository`、`WebSettings`。
- Produces: `AuthService.is_configured()`、`setup()`、`login()`、`authenticate()`、`logout()`；`AuthenticatedSession`。

- [ ] **Step 1：写失败测试**

```python
import pytest

from tmuxbot.control_plane.repository import ControlPlaneRepository
from tmuxbot.web.auth import AuthError, AuthService


def test_auth_service_requires_one_time_setup_and_rotates_session(tmp_path):
    repo = ControlPlaneRepository(tmp_path / "control.sqlite3")
    repo.migrate()
    auth = AuthService(repo, session_ttl_seconds=3600)

    assert auth.is_configured() is False
    session = auth.setup("correct horse battery staple", now=1000)
    assert auth.is_configured() is True
    assert auth.authenticate(session.token, now=1001).csrf_token == session.csrf_token

    with pytest.raises(AuthError):
        auth.authenticate(session.token + "tampered", now=1001)

    with pytest.raises(AuthError):
        auth.setup("another acceptable password", now=1002)
    with pytest.raises(AuthError):
        auth.login("wrong password", now=1003)

    auth.logout(session.token)
    with pytest.raises(AuthError):
        auth.authenticate(session.token, now=1004)
```

- [ ] **Step 2：运行测试并确认失败**

Run: `uv run pytest tests/web/test_auth_service.py -v`

Expected: FAIL，提示 `tmuxbot.web.auth` 不存在。

- [ ] **Step 3：实现 AuthService**

```python
from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass

from itsdangerous import BadSignature, Signer
from pwdlib import PasswordHash

from tmuxbot.control_plane.repository import ControlPlaneRepository


class AuthError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class AuthenticatedSession:
    token: str
    csrf_token: str


class AuthService:
    PASSWORD_KEY = "auth.password_hash"
    SIGNING_KEY = "auth.cookie_signing_key"

    def __init__(self, repository: ControlPlaneRepository, *, session_ttl_seconds: int):
        self.repository = repository
        self.session_ttl_seconds = session_ttl_seconds
        self.password_hash = PasswordHash.recommended()

    @staticmethod
    def _token_hash(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def is_configured(self) -> bool:
        return self.repository.get_setting(self.PASSWORD_KEY) is not None

    def setup(self, password: str, *, now: int) -> AuthenticatedSession:
        if self.is_configured():
            raise AuthError("password is already configured")
        self._validate_password(password)
        self.repository.set_setting(self.PASSWORD_KEY, self.password_hash.hash(password))
        return self._new_session(now)

    def login(self, password: str, *, now: int) -> AuthenticatedSession:
        encoded = self.repository.get_setting(self.PASSWORD_KEY)
        if encoded is None or not self.password_hash.verify(password, encoded):
            raise AuthError("invalid credentials")
        return self._new_session(now)

    def authenticate(self, token: str, *, now: int) -> AuthenticatedSession:
        try:
            self._signer().unsign(token)
        except BadSignature as exc:
            raise AuthError("invalid or expired session") from exc
        csrf = self.repository.get_session(self._token_hash(token), now=now)
        if csrf is None:
            raise AuthError("invalid or expired session")
        return AuthenticatedSession(token=token, csrf_token=csrf)

    def logout(self, token: str) -> None:
        self.repository.delete_session(self._token_hash(token))

    def _new_session(self, now: int) -> AuthenticatedSession:
        token = self._signer().sign(secrets.token_urlsafe(32)).decode("utf-8")
        csrf = secrets.token_urlsafe(24)
        self.repository.create_session(
            self._token_hash(token),
            csrf,
            expires_at=now + self.session_ttl_seconds,
        )
        return AuthenticatedSession(token=token, csrf_token=csrf)

    def _signer(self) -> Signer:
        key = self.repository.get_setting(self.SIGNING_KEY)
        if key is None:
            key = secrets.token_urlsafe(48)
            self.repository.set_setting(self.SIGNING_KEY, key)
        return Signer(key, salt="tmuxbot-web-session")

    @staticmethod
    def _validate_password(password: str) -> None:
        if len(password) < 12:
            raise AuthError("password must contain at least 12 characters")
```

- [ ] **Step 4：运行测试并确认通过**

Run: `uv run pytest tests/web/test_auth_service.py -v`

Expected: 1 passed。

- [ ] **Step 5：提交**

```bash
git add tmuxbot/web/auth.py tests/web/test_auth_service.py
git commit -m "feat(web): add single-user authentication"
```

### Task 5：只读 tmux inventory 与孤儿分类

**Files:**
- Create: `tmuxbot/control_plane/tmux_inventory.py`
- Test: `tests/control_plane/test_tmux_inventory.py`

**Interfaces:**
- Consumes: `Binding`、`TmuxPaneRecord`、`SessionInventoryItem`。
- Produces: `TmuxInventory.list_panes()`、`classify_inventory(panes, bindings, ignored_targets)`。

- [ ] **Step 1：写失败测试**

```python
from pathlib import Path

from tmuxbot.control_plane.models import SessionClass, TmuxPaneRecord
from tmuxbot.control_plane.tmux_inventory import classify_inventory, parse_tmux_rows
from tmuxbot.state import Binding


def test_tmux_inventory_parses_exact_fields_and_classifies_without_mutation():
    rows = "alpha\t0\t1\tpython\t/repo\t4321\nother\t2\t0\tbash\t/tmp\t99\n"
    panes = parse_tmux_rows(rows)
    binding = Binding(
        name="codex-main",
        chat_id=1,
        thread_id=None,
        tmux_session="alpha",
        tmux_window=0,
        tmux_pane=1,
        cwd=Path("/repo"),
        backend="codex",
    )

    items = classify_inventory(panes, [binding], ignored_targets={"other:2.0"})

    assert items[0].classification == SessionClass.MANAGED
    assert items[0].binding_name == "codex-main"
    assert items[0].provider == "codex"
    assert items[1].classification == SessionClass.IGNORED
```

- [ ] **Step 2：运行测试并确认失败**

Run: `uv run pytest tests/control_plane/test_tmux_inventory.py -v`

Expected: FAIL，提示 inventory 模块不存在。

- [ ] **Step 3：实现只读查询和纯函数分类**

```python
from __future__ import annotations

import subprocess
from collections.abc import Iterable

from tmuxbot.control_plane.models import (
    SessionClass,
    SessionInventoryItem,
    TmuxPaneRecord,
)
from tmuxbot.state import Binding


TMUX_FORMAT = "#{session_name}\t#{window_index}\t#{pane_index}\t#{pane_current_command}\t#{pane_current_path}\t#{pane_pid}"


def parse_tmux_rows(output: str) -> list[TmuxPaneRecord]:
    panes: list[TmuxPaneRecord] = []
    for line in output.splitlines():
        if not line:
            continue
        session, window, pane, command, cwd, pid = line.split("\t", 5)
        panes.append(
            TmuxPaneRecord(
                target=f"{session}:{window}.{pane}",
                session_name=session,
                window_index=int(window),
                pane_index=int(pane),
                command=command,
                cwd=cwd,
                pid=int(pid),
            )
        )
    return panes


class TmuxInventory:
    def list_panes(self) -> list[TmuxPaneRecord]:
        result = subprocess.run(
            ["tmux", "list-panes", "-a", "-F", TMUX_FORMAT],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            return []
        return parse_tmux_rows(result.stdout)


def classify_inventory(
    panes: Iterable[TmuxPaneRecord],
    bindings: Iterable[Binding],
    *,
    ignored_targets: set[str],
) -> list[SessionInventoryItem]:
    managed = {binding.tmux_target: binding for binding in bindings}
    items: list[SessionInventoryItem] = []
    for pane in panes:
        binding = managed.get(pane.target)
        if binding is not None:
            classification = SessionClass.MANAGED
        elif pane.target in ignored_targets:
            classification = SessionClass.IGNORED
        else:
            classification = SessionClass.ORPHAN
        items.append(
            SessionInventoryItem(
                pane=pane,
                classification=classification,
                binding_name=None if binding is None else binding.name,
                provider=None if binding is None else binding.backend,
            )
        )
    return items
```

- [ ] **Step 4：运行测试并确认通过**

Run: `uv run pytest tests/control_plane/test_tmux_inventory.py -v`

Expected: 1 passed。

- [ ] **Step 5：提交**

```bash
git add tmuxbot/control_plane/tmux_inventory.py tests/control_plane/test_tmux_inventory.py
git commit -m "feat(control-plane): add read-only tmux inventory"
```

### Task 6：FastAPI 认证边界、事件和 inventory API

**Files:**
- Create: `tmuxbot/web/schemas.py`
- Create: `tmuxbot/web/app.py`
- Test: `tests/web/test_web_app.py`

**Interfaces:**
- Consumes: `WebSettings`、`AuthService`、`ControlPlaneRepository`、`TmuxInventory`、现有 `load_config()` 与 `S.bindings`。
- Produces: `create_app(settings, repository, inventory, bindings) -> FastAPI`。

- [ ] **Step 1：写失败 API 测试**

```python
from fastapi.testclient import TestClient

from tmuxbot.control_plane.repository import ControlPlaneRepository
from tmuxbot.web.app import create_app
from tmuxbot.web.settings import WebSettings


class FakeInventory:
    def list_panes(self):
        return []


def test_web_api_requires_auth_and_csrf(tmp_path):
    settings = WebSettings(
        host="127.0.0.1",
        port=8765,
        database_path=tmp_path / "control.sqlite3",
        secure_cookie=False,
    )
    repo = ControlPlaneRepository(settings.database_path)
    repo.migrate()
    client = TestClient(create_app(settings, repo, FakeInventory(), []))

    assert client.get("/api/health").json() == {"status": "ok"}
    assert client.get("/api/tmux/sessions").status_code == 401

    setup = client.post("/api/auth/setup", json={"password": "correct horse battery staple"})
    assert setup.status_code == 201
    csrf = setup.json()["csrf_token"]
    assert client.get("/api/tmux/sessions").status_code == 200
    assert client.post("/api/auth/logout").status_code == 403
    assert client.post("/api/auth/logout", headers={"X-CSRF-Token": csrf}).status_code == 204
    assert client.get("/api/tmux/sessions").status_code == 401


def test_setup_is_disabled_after_first_password(tmp_path):
    settings = WebSettings("127.0.0.1", 8765, tmp_path / "control.sqlite3", False)
    repo = ControlPlaneRepository(settings.database_path)
    repo.migrate()
    client = TestClient(create_app(settings, repo, FakeInventory(), []))

    assert client.post("/api/auth/setup", json={"password": "correct horse battery staple"}).status_code == 201
    assert client.post("/api/auth/setup", json={"password": "another correct password"}).status_code == 409
```

- [ ] **Step 2：运行测试并确认失败**

Run: `uv run pytest tests/web/test_web_app.py -v`

Expected: FAIL，提示 `tmuxbot.web.app` 不存在。

- [ ] **Step 3：实现请求 schema**

创建 `tmuxbot/web/schemas.py`：

```python
from pydantic import BaseModel, Field


class PasswordRequest(BaseModel):
    password: str = Field(min_length=12, max_length=1024)
```

- [ ] **Step 4：实现 FastAPI 装配和安全依赖**

`tmuxbot/web/app.py` 必须包含以下路由和行为：

```python
from __future__ import annotations

import os
import secrets
import time

from fastapi import Cookie, Depends, FastAPI, Header, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse

from tmuxbot.control_plane.repository import ControlPlaneRepository
from tmuxbot.control_plane.tmux_inventory import TmuxInventory, classify_inventory
from tmuxbot.state import Binding
from tmuxbot.web.auth import AuthError, AuthService, AuthenticatedSession
from tmuxbot.web.schemas import PasswordRequest
from tmuxbot.web.settings import WebSettings


COOKIE_NAME = "tmuxbot_session"


def create_app(
    settings: WebSettings,
    repository: ControlPlaneRepository,
    inventory: TmuxInventory,
    bindings: list[Binding],
) -> FastAPI:
    app = FastAPI(title="tmuxbot control plane", docs_url=None, redoc_url=None)
    auth = AuthService(repository, session_ttl_seconds=settings.session_ttl_seconds)

    def current_session(
        tmuxbot_session: str | None = Cookie(default=None, alias=COOKIE_NAME),
    ) -> AuthenticatedSession:
        if tmuxbot_session is None:
            raise HTTPException(status_code=401, detail="authentication required")
        try:
            return auth.authenticate(tmuxbot_session, now=int(time.time()))
        except AuthError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc

    def csrf_session(
        session: AuthenticatedSession = Depends(current_session),
        csrf: str | None = Header(default=None, alias="X-CSRF-Token"),
    ) -> AuthenticatedSession:
        if csrf is None or not secrets.compare_digest(csrf, session.csrf_token):
            raise HTTPException(status_code=403, detail="invalid csrf token")
        return session

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

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/auth/status")
    def auth_status() -> dict[str, bool]:
        return {"configured": auth.is_configured()}

    @app.post("/api/auth/setup", status_code=status.HTTP_201_CREATED)
    def setup_password(body: PasswordRequest, response: Response) -> dict[str, str]:
        try:
            session = auth.setup(body.password, now=int(time.time()))
        except AuthError as exc:
            code = 409 if auth.is_configured() else 400
            raise HTTPException(status_code=code, detail=str(exc)) from exc
        set_session_cookie(response, session.token)
        return {"csrf_token": session.csrf_token}

    @app.post("/api/auth/login")
    def login(body: PasswordRequest, response: Response) -> dict[str, str]:
        try:
            session = auth.login(body.password, now=int(time.time()))
        except AuthError as exc:
            raise HTTPException(status_code=401, detail="invalid credentials") from exc
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
        after: int = 0,
        limit: int = 100,
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
        items = classify_inventory(inventory.list_panes(), bindings, ignored_targets=set())
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
```

- [ ] **Step 5：运行测试并确认通过**

Run: `uv run pytest tests/web/test_web_app.py -v`

Expected: 2 passed。

- [ ] **Step 6：增加 Origin 中间件测试和实现**

在 `tests/web/test_web_app.py` 增加：

```python
def test_state_changing_request_rejects_foreign_origin(tmp_path):
    settings = WebSettings("127.0.0.1", 8765, tmp_path / "control.sqlite3", False)
    repo = ControlPlaneRepository(settings.database_path)
    repo.migrate()
    client = TestClient(create_app(settings, repo, FakeInventory(), []))

    response = client.post(
        "/api/auth/setup",
        json={"password": "correct horse battery staple"},
        headers={"Origin": "https://attacker.example"},
    )
    assert response.status_code == 403
```

在 `create_app()` 创建 `auth` 之后、注册路由之前加入：

```python
    configured_origin = os.getenv("TMUXBOT_WEB_PUBLIC_ORIGIN")
    allowed_origin = configured_origin or f"http://{settings.host}:{settings.port}"

    @app.middleware("http")
    async def enforce_origin(request: Request, call_next):
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            if origin is not None and origin.rstrip("/") != allowed_origin.rstrip("/"):
                return JSONResponse(status_code=403, content={"detail": "invalid origin"})
        return await call_next(request)
```

测试环境未携带 Origin 的直接 API 调用继续允许；浏览器发出的跨源写请求会被拒绝。

- [ ] **Step 7：运行 Web 测试**

Run: `uv run pytest tests/web -v`

Expected: 全部通过。

- [ ] **Step 8：提交**

```bash
git add tmuxbot/web tests/web
git commit -m "feat(web): expose authenticated foundation API"
```

### Task 7：独立 `tmuxbot web` 入口与部署说明

**Files:**
- Create: `tmuxbot/web/__main__.py`
- Create: `deploy/systemd/tmuxbot-web.service`
- Modify: `tmuxbot/__main__.py`
- Modify: `.env.example`
- Modify: `DEVELOPMENT.md`
- Modify: `README.md`
- Test: `tests/web/test_web_entrypoint.py`

**Interfaces:**
- Consumes: `WebSettings`、`load_config()`、`ControlPlaneRepository`、`TmuxInventory`、`create_app()`。
- Produces: `tmuxbot web` 与 `python -m tmuxbot.web` 独立启动路径。

- [ ] **Step 1：写失败入口测试**

```python
from tmuxbot.__main__ import build_parser


def test_cli_exposes_web_subcommand_without_changing_default_runtime():
    parser = build_parser()
    assert parser.parse_args([]).command == "bridge"
    assert parser.parse_args(["web"]).command == "web"
```

- [ ] **Step 2：运行测试并确认失败**

Run: `uv run pytest tests/web/test_web_entrypoint.py -v`

Expected: FAIL，提示 `build_parser` 不存在。

- [ ] **Step 3：提取 CLI parser 并保持旧行为**

在 `tmuxbot/__main__.py` 中新增：

```python
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tmuxbot",
        description="Telegram/Feishu/WebUI <-> tmux AI CLI control plane",
    )
    parser.add_argument("--version", action="version", version=f"tmuxbot {__version__}")
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("bridge", help="run Telegram and Feishu bridge")
    subparsers.add_parser("web", help="run the WebUI control plane")
    parser.set_defaults(command="bridge")
    return parser


def run(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    if args.command == "web":
        from tmuxbot.web.__main__ import run_web

        run_web()
        return
    asyncio.run(main())
```

- [ ] **Step 4：实现 Web 进程装配**

创建 `tmuxbot/web/__main__.py`：

```python
from __future__ import annotations

import uvicorn

from tmuxbot.__main__ import BINDINGS_FILE, ENV_FILE, OFFSETS_FILE
from tmuxbot.config import load_config
from tmuxbot.control_plane.repository import ControlPlaneRepository
from tmuxbot.control_plane.tmux_inventory import TmuxInventory
from tmuxbot.state import S
from tmuxbot.web.app import create_app
from tmuxbot.web.settings import WebSettings


def build_app():
    settings = WebSettings.from_env()
    load_config(ENV_FILE, BINDINGS_FILE, OFFSETS_FILE)
    repository = ControlPlaneRepository(settings.database_path)
    repository.migrate()
    return settings, create_app(settings, repository, TmuxInventory(), S.bindings)


def run_web() -> None:
    settings, app = build_app()
    uvicorn.run(app, host=settings.host, port=settings.port, proxy_headers=False)


if __name__ == "__main__":
    run_web()
```

- [ ] **Step 5：增加 systemd 与环境示例**

`deploy/systemd/tmuxbot-web.service` 使用独立进程、现有项目目录和环境文件：

```ini
[Unit]
Description=tmuxbot Web control plane
After=network.target

[Service]
Type=simple
WorkingDirectory=/home/pyadmin/claude-project/tmuxbot
EnvironmentFile=/home/pyadmin/claude-project/tmuxbot/.env
ExecStart=/home/pyadmin/claude-project/tmuxbot/.venv/bin/tmuxbot web
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
```

`.env.example` 增加：

```dotenv
TMUXBOT_WEB_HOST=127.0.0.1
TMUXBOT_WEB_PORT=8765
TMUXBOT_WEB_SECURE_COOKIE=false
# TMUXBOT_WEB_PUBLIC_ORIGIN=https://tmuxbot.example.com
```

README 和 DEVELOPMENT 明确：Web 服务独立启动；本阶段只有认证后的只读 inventory/event API；不要直接将端口暴露到公网。

- [ ] **Step 6：运行入口和回归测试**

Run: `uv run pytest tests/web/test_web_entrypoint.py tests/test_project_metadata.py -v`

Expected: 全部通过，现有 `tmuxbot --version` 行为不变。

- [ ] **Step 7：运行全量验证**

Run: `make check`

Expected: compile、pytest、ruff 全部通过。

Run: `git diff --check`

Expected: 无输出，退出码 0。

- [ ] **Step 8：提交阶段成果**

```bash
git add tmuxbot deploy/systemd/tmuxbot-web.service .env.example README.md DEVELOPMENT.md tests pyproject.toml uv.lock
git commit -m "feat(web): deliver control-plane foundation"
git push
```

## 规格覆盖自检

- Phase 1 领域模型：Task 2。
- SQLite、编号 migration 和崩溃后持久化：Task 3。
- 追加式、幂等 `RunEvent`：Task 2–3、Task 6。
- 首次密码、Argon2、HTTP-only Cookie、CSRF 和 Origin：Task 4、Task 6。
- 只读 tmux inventory 和 managed/orphan/ignored 分类：Task 5–6。
- Web 独立进程、不影响 Telegram/飞书：Task 7。
- 默认 localhost 和公网暴露警告：Task 1、Task 7。

本阶段不实现终端 PTY、会话 adopt/archive、调度器和前端页面；这些能力分别属于后续阶段，Phase 1 只建立其稳定、安全的数据和 API 基础。
