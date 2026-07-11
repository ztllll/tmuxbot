from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from tmuxbot.paths import RuntimePaths
from tmuxbot.supervisor import BridgeSupervisor, inspect_bridge_readiness


def _paths(tmp_path: Path) -> RuntimePaths:
    return RuntimePaths.discover({}, home=tmp_path)


def test_readiness_is_unconfigured_without_bindings(tmp_path: Path) -> None:
    readiness = inspect_bridge_readiness(_paths(tmp_path), {})
    assert readiness.runnable is False
    assert readiness.reason == "bindings_missing"
    assert readiness.binding_count == 0


def test_readiness_is_degraded_for_invalid_yaml(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    paths.ensure_private_directories()
    paths.bindings_file.write_text("bindings: [\n", encoding="utf-8")
    readiness = inspect_bridge_readiness(paths, {})
    assert readiness.runnable is False
    assert readiness.reason == "config_invalid"


def test_supervisor_does_not_spawn_when_unconfigured(tmp_path: Path) -> None:
    calls: list[tuple[list[str], dict[str, str]]] = []

    async def spawn(argv: list[str], env: dict[str, str]):
        calls.append((argv, env))
        raise AssertionError("must not spawn")

    supervisor = BridgeSupervisor(_paths(tmp_path), {}, spawn=spawn)

    async def scenario() -> None:
        stop = asyncio.Event()
        task = asyncio.create_task(supervisor.run(stop, poll_interval=0.01))
        await asyncio.sleep(0.03)
        stop.set()
        await task

    asyncio.run(scenario())
    assert calls == []
    assert supervisor.snapshot()["state"] == "stopped"


def test_bridge_argv_and_setup_grant_not_in_child_environment(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    paths.ensure_private_directories()
    paths.bindings_file.write_text(
        "bindings:\n"
        "  - name: demo\n"
        "    chat_id: 1\n"
        "    thread_id: 0\n"
        "    bot_token_env: TG_CODEX_BOT_TOKEN\n"
        "    backend: codex\n"
        "    tmux_session: demo\n"
        "    cwd: /tmp\n",
        encoding="utf-8",
    )
    environ = {"TG_CODEX_BOT_TOKEN": "123:abc", "TMUXBOT_SETUP_GRANT": "secret"}
    observed: list[tuple[list[str], dict[str, str]]] = []

    class Child:
        returncode = None

        async def wait(self) -> int:
            self.returncode = 0
            return 0

        def terminate(self) -> None:
            self.returncode = 0

    async def spawn(argv: list[str], env: dict[str, str]):
        observed.append((argv, env))
        return Child()

    supervisor = BridgeSupervisor(paths, environ, spawn=spawn)

    async def scenario() -> None:
        stop = asyncio.Event()
        task = asyncio.create_task(supervisor.run(stop, poll_interval=0.01))
        await asyncio.sleep(0.03)
        stop.set()
        await task

    asyncio.run(scenario())
    assert observed
    assert observed[0][0] == [sys.executable, "-m", "tmuxbot", "bridge"]
    assert "TMUXBOT_SETUP_GRANT" not in observed[0][1]
