# Z1 零配置启动与 Preview A 实施计划

> **Agent 执行要求：** 必须使用 `superpowers:subagent-driven-development`（推荐）或 `superpowers:executing-plans` 逐任务实施。每个任务严格执行 RED → GREEN → review。

**目标：** 交付 `uv tool install 'tmuxbot[full]'` 与 `tmuxbot serve --open`，在空 HOME、无 `.env`、无 `bindings.yaml`、无通道配置时仍能打开中文 WebUI，完成首次设置并查看系统、bridge、Provider 候选和 tmux 状态。

**架构：** XDG `RuntimePaths` 统一产品路径；Web bootstrap 与严格 bridge 配置生命周期拆分；`serve` 始终运行 Web，并用独立 child process 管理 bridge；React/Vite 产物打进 wheel，由 FastAPI 服务。tmux session 不属于 supervisor 子进程生命周期。

**技术栈：** Python 3.10+、FastAPI、Uvicorn、SQLite、React 19、TypeScript、Vite、Vitest、Testing Library、pytest、Hatch。

## 全局约束

- `tmuxbot serve --open` 默认监听 `127.0.0.1`。
- 无 `.env`、无 bindings 和无通道 credential 时 Web 必须保持健康。
- 裸 `tmuxbot bridge` 保持严格 fail-fast，兼容现有运维与安全边界。
- Web 进程不得获取 bridge lock、启动 polling 或创建 tmux session。
- supervisor 只管理 bridge child，不得 kill tmux session。
- 自动 setup grant 只存在内存、10 分钟有效、成功后立即消费，不进入 query、日志、SQLite、环境或 child process。
- 浏览器值不能直接变成 shell command、binary path 或 tmux target。
- 现有 `TMUXBOT_*` override 优先级和源码 checkout 旧部署必须兼容。
- WebUI 中文优先，视觉遵循已批准的 Cloud Steel/Run Spine 工业控制面；Z1 不实现完整调度图和终端。

---

### Task 1：统一 XDG RuntimePaths

**Files:**
- Create: `tmuxbot/paths.py`
- Modify: `tmuxbot/__main__.py`
- Modify: `tmuxbot/web/__main__.py`
- Modify: `tmuxbot/web/settings.py`
- Modify: `tmuxbot/hooks/claude.py`
- Test: `tests/test_paths.py`
- Test: `tests/web/test_web_settings.py`

**Interfaces:**
- Produces: `RuntimePaths.discover(environ, home=None, legacy_project_dir=None)`。
- Produces: `RuntimePaths.ensure_private_directories()`。
- Consumers: bridge、web、serve、doctor、hook spool。

```python
@dataclass(frozen=True, slots=True)
class RuntimePaths:
    config_dir: Path
    data_dir: Path
    state_dir: Path
    env_file: Path
    bindings_file: Path
    database_file: Path
    offsets_file: Path
    lock_file: Path
    hook_spool_file: Path

    @classmethod
    def discover(
        cls,
        environ: Mapping[str, str],
        *,
        home: Path | None = None,
        legacy_project_dir: Path | None = None,
    ) -> "RuntimePaths": ...

    def ensure_private_directories(self) -> None: ...
```

- [ ] **Step 1：写路径优先级失败测试**

覆盖：

```python
def test_paths_default_to_xdg_under_empty_home(tmp_path): ...
def test_explicit_tmuxbot_overrides_win_over_xdg(tmp_path): ...
def test_explicit_xdg_paths_work_when_home_is_empty(tmp_path): ...
def test_installed_package_never_uses_site_packages_as_config_root(tmp_path): ...
def test_source_checkout_legacy_files_are_used_only_when_present(tmp_path): ...
def test_private_directories_are_created_with_mode_0700(tmp_path): ...
```

精确默认：

```text
~/.config/tmuxbot/.env
~/.config/tmuxbot/bindings.yaml
~/.local/share/tmuxbot/control-plane.sqlite3
~/.local/state/tmuxbot/offsets.json
~/.local/state/tmuxbot/tmuxbot.lock
~/.local/state/tmuxbot/claude-hooks.jsonl
```

- [ ] **Step 2：运行 RED**

Run: `uv run pytest tests/test_paths.py tests/web/test_web_settings.py -v`

Expected: FAIL，`tmuxbot.paths` 不存在或仍使用源码根路径。

- [ ] **Step 3：实现 RuntimePaths**

优先级：

1. `TMUXBOT_ENV`、`TMUXBOT_BINDINGS`、`TMUXBOT_DATA_DIR` 和后续精确 override。
2. `XDG_CONFIG_HOME`、`XDG_DATA_HOME`、`XDG_STATE_HOME`。
3. HOME 下 XDG 默认。
4. 仅当 `legacy_project_dir/.env` 或 `bindings.yaml` 真实存在时，对对应文件使用 legacy 路径。

目录创建使用 `parents=True`，最终目录验证非 symlink 并 chmod `0700`。

- [ ] **Step 4：消除 import-time 源码路径常量**

`tmuxbot.__main__`、`tmuxbot.web.__main__` 和 Claude hook 在运行时接收/发现 `RuntimePaths`。Provider 自有 `~/.claude`、`~/.codex` 路径保持不变。

- [ ] **Step 5：运行 GREEN 和回归**

Run: `uv run pytest tests/test_paths.py tests/web/test_web_settings.py tests/test_claude_hooks.py tests/test_project_metadata.py -v`

Expected: 全部通过。

- [ ] **Step 6：提交**

```bash
git add tmuxbot/paths.py tmuxbot/__main__.py tmuxbot/web/__main__.py tmuxbot/web/settings.py tmuxbot/hooks/claude.py tests/test_paths.py tests/web/test_web_settings.py
git commit -m "feat(runtime): adopt XDG application paths"
```

### Task 2：拆分 Web bootstrap 与严格 bridge 配置

**Files:**
- Modify: `tmuxbot/config.py`
- Modify: `tmuxbot/validation.py`
- Modify: `tmuxbot/web/__main__.py`
- Test: `tests/test_config_loading.py`
- Modify: `tests/test_validation.py`
- Modify: `tests/web/test_web_entrypoint.py`

**Interfaces:**

```python
def load_config(
    env_file: Path,
    bindings_file: Path,
    offsets_file: Path,
    *,
    allow_missing_bindings: bool = False,
    allow_empty_bindings: bool = False,
) -> None: ...

def validate_bindings(
    bindings: list[Binding],
    *,
    require_nonempty: bool = True,
) -> None: ...
```

- [ ] **Step 1：写缺失和空配置 RED 测试**

```python
def test_web_config_allows_missing_env_and_bindings(tmp_path): ...
def test_web_config_allows_bindings_empty_list(tmp_path): ...
def test_bridge_config_still_rejects_missing_bindings(tmp_path): ...
def test_invalid_yaml_never_becomes_unconfigured(tmp_path): ...
def test_failed_reload_does_not_partially_mutate_global_state(tmp_path): ...
```

- [ ] **Step 2：运行 RED**

Run: `uv run pytest tests/test_config_loading.py tests/test_validation.py tests/web/test_web_entrypoint.py -v`

- [ ] **Step 3：实现局部解析与原子状态替换**

先解析 `boss_user_id/setup_mode/bindings/offsets` 到局部值，完成校验后一次性替换 `S`。缺失 env 合法；仅在显式 allow flag 下缺失或空 bindings 合法。YAML 语法/类型错误仍抛 `ConfigValidationError`。

- [ ] **Step 4：Web build 使用宽松模式，bridge 保持默认严格**

`build_app()` 调用：

```python
load_config(
    paths.env_file,
    paths.bindings_file,
    paths.offsets_file,
    allow_missing_bindings=True,
    allow_empty_bindings=True,
)
```

- [ ] **Step 5：GREEN、回归、提交**

Run: `uv run pytest tests/test_config_loading.py tests/test_validation.py tests/web/test_web_entrypoint.py tests/test_lifecycle.py -v`

```bash
git add tmuxbot/config.py tmuxbot/validation.py tmuxbot/web/__main__.py tests/test_config_loading.py tests/test_validation.py tests/web/test_web_entrypoint.py
git commit -m "feat(web): allow empty bootstrap configuration"
```

### Task 3：内存型短期一次性 SetupGrant

**Files:**
- Create: `tmuxbot/web/setup.py`
- Modify: `tmuxbot/web/app.py`
- Modify: `tmuxbot/web/__main__.py`
- Modify: `tmuxbot/web/settings.py`
- Create: `tests/web/test_setup_grant.py`
- Modify: `tests/web/test_web_app.py`

**Interfaces:**

```python
SETUP_GRANT_TTL_SECONDS = 600

@dataclass(slots=True)
class SetupGrant:
    token: str
    expires_at: int
    consumed: bool = False

    @classmethod
    def generate(cls, *, now: int, ttl_seconds: int = 600) -> "SetupGrant": ...
    def is_available(self, *, now: int) -> bool: ...
    def authorize(self, submitted: str, *, now: int) -> bool: ...
    def consume(self) -> None: ...
```

`create_app()` 兼容扩展：

```python
def create_app(
    settings: WebSettings,
    repository: ControlPlaneRepository,
    inventory: TmuxInventory,
    bindings: list[Binding],
    *,
    setup_grant: SetupGrant | None = None,
    bridge_status: Callable[[], Mapping[str, object]] | None = None,
) -> FastAPI: ...
```

- [ ] **Step 1：写 grant RED 测试**

覆盖生成长度、常量时间校验、过期、错误 token、consume、重放和线程内状态。

- [ ] **Step 2：写 API RED 测试**

- 本机 + CSRF + 有效 grant setup 成功。
- grant 过期/错误/已消费固定 403。
- setup 成功立即 consume。
- `/api/auth/status` 只返回 `setup_available` 和 `setup_expires_at`，不返回 token。
- 已配置数据库不生成 grant。
- legacy `TMUXBOT_WEB_SETUP_TOKEN` 继续作为显式 override，但不进入新默认流程。

- [ ] **Step 3：实现并运行 GREEN**

Run: `uv run pytest tests/web/test_setup_grant.py tests/web/test_web_app.py tests/web/test_web_settings.py -v`

- [ ] **Step 4：提交**

```bash
git add tmuxbot/web/setup.py tmuxbot/web/app.py tmuxbot/web/__main__.py tmuxbot/web/settings.py tests/web/test_setup_grant.py tests/web/test_web_app.py
git commit -m "feat(web): add ephemeral first-run setup grant"
```

### Task 4：`serve` supervisor 与 bridge child

**Files:**
- Create: `tmuxbot/bridge.py`
- Create: `tmuxbot/supervisor.py`
- Create: `tmuxbot/serve.py`
- Modify: `tmuxbot/__main__.py`
- Modify: `tmuxbot/web/__main__.py`
- Modify: `tmuxbot/web/app.py`
- Create: `tests/test_bridge_supervisor.py`
- Create: `tests/test_serve.py`
- Modify: `tests/web/test_web_entrypoint.py`

**Interfaces:**

```python
@dataclass(frozen=True, slots=True)
class BridgeReadiness:
    runnable: bool
    reason: str
    binding_count: int
    frontend_count: int

def inspect_bridge_readiness(
    paths: RuntimePaths,
    environ: Mapping[str, str],
) -> BridgeReadiness: ...

class BridgeSupervisor:
    async def run(self, stop: asyncio.Event) -> None: ...
    async def stop(self) -> None: ...
    def snapshot(self) -> Mapping[str, object]: ...
```

状态：`unconfigured`、`starting`、`running`、`degraded`、`stopped`。

- [ ] **Step 1：提取 bridge 入口且保持兼容测试**

`tmuxbot bridge` 与无子命令继续严格运行现有 bridge；`tmuxbot.py` 继续作为源码 legacy 入口。

- [ ] **Step 2：写 supervisor RED 测试**

- 无 bindings 不创建 child，状态 `unconfigured`。
- invalid config 状态 `degraded`，Web 不退出。
- runnable 时 argv 精确为 `[sys.executable, "-m", "tmuxbot", "bridge"]`。
- child crash 使用有上限退避，停止时 terminate/wait/reap。
- child 环境不含自动 setup grant。
- supervisor 不调用 tmux kill/new-session。

- [ ] **Step 3：写 `serve --open` RED 测试**

- Web 总是启动。
- 只对 loopback URL 调用 `webbrowser.open()`。
- host `0.0.0.0` 时打开 `127.0.0.1`。
- setup URL 使用 fragment `#grant=...`，不使用 query；不得写日志。
- listener 未 ready 前不打开浏览器。

- [ ] **Step 4：实现 child supervisor**

父进程不导入/重建 bridge 全局 `S`；bridge 是独立 child process。Web app 通过 `bridge_status` callback 获取 snapshot，`/api/system/status` 返回状态但 `/api/health` 保持 Web 健康语义。

- [ ] **Step 5：GREEN 和提交**

Run: `uv run pytest tests/test_bridge_supervisor.py tests/test_serve.py tests/web/test_web_entrypoint.py tests/test_project_metadata.py -v`

```bash
git add tmuxbot/bridge.py tmuxbot/supervisor.py tmuxbot/serve.py tmuxbot/__main__.py tmuxbot/web/__main__.py tmuxbot/web/app.py tests/test_bridge_supervisor.py tests/test_serve.py tests/web/test_web_entrypoint.py
git commit -m "feat(runtime): add unified serve supervisor"
```

### Task 5：Doctor 与 system status API

**Files:**
- Create: `tmuxbot/doctor.py`
- Modify: `tmuxbot/__main__.py`
- Modify: `tmuxbot/web/app.py`
- Create: `tests/test_doctor.py`
- Modify: `tests/web/test_web_app.py`

**Interfaces:**

```python
@dataclass(frozen=True, slots=True)
class CheckResult:
    name: str
    status: Literal["ok", "warning", "error"]
    summary: str
    details: Mapping[str, object]

@dataclass(frozen=True, slots=True)
class DoctorReport:
    checks: tuple[CheckResult, ...]
    exit_code: int

def run_doctor(paths: RuntimePaths, environ: Mapping[str, str]) -> DoctorReport: ...
```

- [ ] **Step 1：写 doctor RED 测试**

检查 Python、tmux 版本、XDG 路径、SQLite quick_check/schema、Claude/Codex PATH/version、legacy migration 状态和 bridge readiness。Provider 只运行 allowlisted `--version`，3 秒 timeout，无模型调用。

- [ ] **Step 2：验证脱敏与 JSON schema**

`doctor --json` 不包含 token、setup grant、password hash、完整环境变量。warning 返回退出码 0，必需项 error 返回 1。

- [ ] **Step 3：增加 `/api/system/status`**

认证后返回 host、paths、bridge、tmux 和 Provider 候选摘要。只返回 server-side ID/掩码，不返回 secret 或任意环境内容。

- [ ] **Step 4：GREEN 和提交**

Run: `uv run pytest tests/test_doctor.py tests/web/test_web_app.py -v`

```bash
git add tmuxbot/doctor.py tmuxbot/__main__.py tmuxbot/web/app.py tests/test_doctor.py tests/web/test_web_app.py
git commit -m "feat(runtime): add doctor and system status"
```

### Task 6：React 中文 Preview A

**Files:**
- Create: `webui/package.json`
- Create: `webui/tsconfig.json`
- Create: `webui/vite.config.ts`
- Create: `webui/src/main.tsx`
- Create: `webui/src/app/App.tsx`
- Create: `webui/src/app/api.ts`
- Create: `webui/src/styles/tokens.css`
- Create: `webui/src/styles/app.css`
- Create: `webui/src/features/auth/SetupView.tsx`
- Create: `webui/src/features/auth/LoginView.tsx`
- Create: `webui/src/features/overview/CommandCenterPreview.tsx`
- Create: `webui/src/**/*.test.tsx`
- Modify: `tmuxbot/web/app.py`
- Create: `tests/web/test_static_app.py`

**Interfaces:**
- Consumes: `/api/auth/status`、setup/login/logout、`/api/system/status`、`/api/tmux/sessions`。
- Produces: `webui/dist`，复制/输出到 `tmuxbot/web/static`。

- [ ] **Step 1：建立视觉 tokens**

```css
:root {
  --cloud-steel: #e7ebef;
  --ink: #17212b;
  --circuit-blue: #3157c8;
  --work-amber: #d59620;
  --outcome-green: #18866b;
  --fault-red: #c84848;
}
```

字体：IBM Plex Sans、Noto Sans SC、IBM Plex Mono；无法联网加载时使用系统 fallback，wheel 不依赖远程字体。

- [ ] **Step 2：写前端 RED 测试**

- 未配置显示中文首次设置和 grant 状态。
- 已配置未登录显示登录。
- 登录后显示主机/bridge/Provider/tmux 状态。
- `unconfigured` 使用明确下一步，不用空白卡片。
- 错误显示原因和恢复动作。
- 键盘 focus、reduced motion、移动端单列。

- [ ] **Step 3：实现 Preview A**

主屏使用轻工业控制面，唯一标志元素为简化 Run Spine：`安装 → 认证 → Provider → 通道 → 会话`，节点显示真实完成状态。Z1 不绘制虚假 Agent DAG。

- [ ] **Step 4：构建并由 FastAPI 服务**

FastAPI：

- `/assets/*` 静态资源。
- 非 `/api`、`/assets` 路径返回 `index.html`，支持 SPA routing。
- 静态文件缺失时 API 仍可运行，并在根路径返回明确构建错误，不影响 bridge。
- 禁止目录遍历。

- [ ] **Step 5：运行前后端测试**

Run: `cd webui && npm ci && npm test -- --run && npm run build`

Run: `uv run pytest tests/web/test_static_app.py tests/web/test_web_app.py -v`

- [ ] **Step 6：提交**

```bash
git add webui tmuxbot/web/static tmuxbot/web/app.py tests/web/test_static_app.py
git commit -m "feat(web): add Chinese first-run command center"
```

### Task 7：Wheel、full extra、service 与零配置 E2E

**Files:**
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Modify: `deploy/systemd/tmuxbot.service`
- Modify: `deploy/systemd/tmuxbot-web.service`
- Modify: `bin/restart.sh`
- Modify: `bin/status.sh`
- Modify: `bin/stop.sh`
- Modify: `tmuxbot.py`
- Modify: `README.md`
- Modify: `DEVELOPMENT.md`
- Modify: `RELEASE.md`
- Create: `tests/test_wheel_contents.py`
- Create: `tests/e2e/test_zero_config_startup.py`

**Interfaces:**

`full` 精确包含 Web 与飞书运行依赖。wheel 必须包含 Python package、`tmuxbot/web/static` 和 console entrypoint。

- [ ] **Step 1：写 wheel RED 测试**

真实构建 wheel 并解包，断言：

- `tmuxbot` console script metadata。
- `full` extra metadata。
- `tmuxbot/web/static/index.html` 与 hashed assets。
- 无 `.env`、数据库、bindings 或 secret 被打包。

- [ ] **Step 2：写空 HOME E2E**

在临时 HOME/XDG 下安装 wheel，运行 `tmuxbot serve`：

- 无 legacy 文件也持续运行。
- health/status 成功。
- bridge=`unconfigured`。
- XDG dirs `0700`，数据库 `0600`。
- SIGTERM 后 child 被 reap，未调用 tmux kill。

- [ ] **Step 3：更新 service 和 legacy scripts**

推荐 user service：

```ini
EnvironmentFile=-%h/.config/tmuxbot/.env
ExecStart=%h/.local/bin/tmuxbot serve
StandardOutput=journal
StandardError=journal
WantedBy=default.target
```

`tmuxbot-web.service` 标记兼容模式；bin scripts 优先调用安装后的 `tmuxbot`，源码 fallback 明确标注 development-only。

- [ ] **Step 4：文档用户路径**

README 首屏只保留：

```bash
uv tool install 'tmuxbot[full]'
tmuxbot serve --open
```

源码开发、legacy bridge、systemd 和迁移放到后续章节。

- [ ] **Step 5：全量验证**

Run: `uv sync --extra dev --extra web --extra feishu`

Run: `make check`

Run: `cd webui && npm ci && npm test -- --run && npm run build`

Run: `uv run pytest tests/e2e/test_zero_config_startup.py tests/test_wheel_contents.py -v`

Run: `git diff --check`

- [ ] **Step 6：提交和推送**

```bash
git add pyproject.toml uv.lock deploy bin tmuxbot.py README.md DEVELOPMENT.md RELEASE.md tests
git commit -m "feat: deliver zero-config tmuxbot Preview A"
git push origin productization-prep
```
