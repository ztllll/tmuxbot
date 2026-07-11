from __future__ import annotations

import asyncio
import os
import signal
import webbrowser
from collections.abc import Mapping
from pathlib import Path

import uvicorn

from tmuxbot.paths import RuntimePaths
from tmuxbot.supervisor import BridgeSupervisor
from tmuxbot.web.__main__ import build_app


def browser_url(host: str, port: int, grant: str | None = None) -> str:
    browser_host = "127.0.0.1" if host in {"0.0.0.0", "::", "localhost"} else host
    url = f"http://{browser_host}:{port}/"
    return f"{url}#grant={grant}" if grant else url


async def serve(
    *,
    open_browser: bool = False,
    environ: Mapping[str, str] | None = None,
    legacy_project_dir: Path | None = None,
) -> None:
    settings, app = build_app()
    runtime_env = dict(os.environ if environ is None else environ)
    paths = RuntimePaths.discover(
        runtime_env,
        legacy_project_dir=legacy_project_dir
        or Path(__file__).resolve().parent.parent,
    )
    paths.ensure_private_directories()
    supervisor = BridgeSupervisor(paths, runtime_env)
    app.state.bridge_status = supervisor.snapshot
    stop = asyncio.Event()

    config = uvicorn.Config(
        app,
        host=settings.host,
        port=settings.port,
        proxy_headers=False,
        log_level=runtime_env.get("LOG_LEVEL", "info").lower(),
    )
    server = uvicorn.Server(config)
    server.install_signal_handlers = lambda: None

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):
            pass

    bridge_task = asyncio.create_task(supervisor.run(stop))
    web_task = asyncio.create_task(server.serve())
    while not server.started and not web_task.done():
        await asyncio.sleep(0.02)

    if server.started:
        grant_obj = getattr(app.state, "setup_grant", None)
        grant = None if grant_obj is None else grant_obj.token
        local_url = browser_url(settings.host, settings.port, grant)
        if grant is not None:
            print(f"首次设置地址（10 分钟内有效）: {local_url}", flush=True)
        else:
            print(f"WebUI: {local_url}", flush=True)
        if open_browser:
            webbrowser.open(local_url)

    stop_task = asyncio.create_task(stop.wait())
    done, _ = await asyncio.wait(
        {web_task, stop_task}, return_when=asyncio.FIRST_COMPLETED
    )
    if stop_task in done:
        server.should_exit = True
    await web_task
    stop.set()
    await bridge_task


def run_serve(*, open_browser: bool = False) -> None:
    asyncio.run(serve(open_browser=open_browser))
