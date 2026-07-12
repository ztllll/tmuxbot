from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from dotenv import dotenv_values

from tmuxbot.paths import RuntimePaths
from tmuxbot.state import Binding
from tmuxbot.validation import ConfigValidationError, validate_bindings


@dataclass(frozen=True, slots=True)
class BridgeReadiness:
    runnable: bool
    reason: str
    binding_count: int
    frontend_count: int


def _binding_from_mapping(item: Mapping[str, Any]) -> Binding:
    raw_chat_id = item.get("chat_id", 0)
    chat_id: int | str = (
        int(raw_chat_id)
        if str(raw_chat_id).lstrip("-").isdigit()
        else str(raw_chat_id)
    )
    return Binding(
        name=str(item.get("name", "")),
        chat_id=chat_id,
        thread_id=item.get("thread_id"),
        tmux_session=str(item.get("tmux_session", "")),
        tmux_window=int(item.get("tmux_window", 0)),
        tmux_pane=int(item.get("tmux_pane", 0)),
        cwd=Path(str(item.get("cwd", ""))),
        backend=str(item.get("backend", "claude_code")),
        bot_token_env=str(item.get("bot_token_env", "TG_BOT_TOKEN")),
        channel=str(item.get("channel", "telegram")),
    )


def inspect_bridge_readiness(
    paths: RuntimePaths, environ: Mapping[str, str]
) -> BridgeReadiness:
    effective_environ = dict(environ)
    if paths.env_file.is_file():
        effective_environ.update(
            {key: value for key, value in dotenv_values(paths.env_file).items() if value is not None}
        )
    if not paths.bindings_file.exists():
        return BridgeReadiness(False, "bindings_missing", 0, 0)
    try:
        raw = yaml.safe_load(paths.bindings_file.read_text(encoding="utf-8")) or {}
        raw_bindings = raw.get("bindings", [])
        if not isinstance(raw_bindings, list):
            raise ConfigValidationError(["bindings must be a list"])
        bindings = [_binding_from_mapping(item) for item in raw_bindings]
        validate_bindings(bindings)
    except (OSError, TypeError, ValueError, yaml.YAMLError, ConfigValidationError):
        return BridgeReadiness(False, "config_invalid", 0, 0)

    frontends: set[tuple[str, str]] = set()
    for binding in bindings:
        if binding.channel == "telegram":
            token = effective_environ.get(binding.bot_token_env, "")
            if token and ":" in token:
                frontends.add((binding.channel, binding.bot_token_env))
        else:
            key = binding.bot_token_env
            if effective_environ.get(f"{key}_APP_ID") and effective_environ.get(f"{key}_APP_SECRET"):
                frontends.add((binding.channel, key))
    if not frontends:
        return BridgeReadiness(False, "credentials_missing", len(bindings), 0)
    return BridgeReadiness(True, "ready", len(bindings), len(frontends))


Spawn = Callable[[list[str], dict[str, str]], Awaitable[Any]]


async def _spawn(argv: list[str], env: dict[str, str]) -> asyncio.subprocess.Process:
    return await asyncio.create_subprocess_exec(*argv, env=env)


class BridgeSupervisor:
    def __init__(
        self,
        paths: RuntimePaths,
        environ: Mapping[str, str],
        *,
        spawn: Spawn = _spawn,
    ) -> None:
        self.paths = paths
        self.environ = dict(environ)
        self.spawn = spawn
        self.pid_file = Path(
            self.environ.get(
                "TMUXBOT_BRIDGE_PID_FILE",
                str(self.paths.state_dir / "bridge.pid"),
            )
        )
        self._child: Any | None = None
        self._state = "unconfigured"
        self._reason = "not_started"
        self._restart_count = 0

    def snapshot(self) -> Mapping[str, object]:
        return {
            "state": self._state,
            "reason": self._reason,
            "restart_count": self._restart_count,
            "pid": getattr(self._child, "pid", None),
        }

    async def run(
        self,
        stop: asyncio.Event,
        *,
        poll_interval: float = 1.0,
        max_backoff: float = 30.0,
    ) -> None:
        backoff = max(poll_interval, 0.01)
        try:
            while not stop.is_set():
                readiness = inspect_bridge_readiness(self.paths, self.environ)
                if not readiness.runnable:
                    self._state = (
                        "degraded" if readiness.reason == "config_invalid" else "unconfigured"
                    )
                    self._reason = readiness.reason
                    try:
                        await asyncio.wait_for(stop.wait(), timeout=poll_interval)
                    except asyncio.TimeoutError:
                        continue
                    break

                child_env = dict(self.environ)
                if self.paths.env_file.is_file():
                    child_env.update(
                        {
                            key: value
                            for key, value in dotenv_values(self.paths.env_file).items()
                            if value is not None
                        }
                    )
                for key in tuple(child_env):
                    if "SETUP_GRANT" in key:
                        child_env.pop(key, None)
                child_env["TMUXBOT_ENV"] = str(self.paths.env_file)
                child_env["TMUXBOT_BINDINGS"] = str(self.paths.bindings_file)
                child_env["TMUXBOT_DATA_DIR"] = str(self.paths.state_dir)
                self._state = "starting"
                self._reason = "spawning"
                try:
                    self._child = await self.spawn(
                        [sys.executable, "-m", "tmuxbot", "bridge"], child_env
                    )
                    self._write_bridge_pid(getattr(self._child, "pid", None))
                    self._state = "running"
                    self._reason = "ready"
                    wait_task = asyncio.create_task(self._child.wait())
                    stop_task = asyncio.create_task(stop.wait())
                    done, pending = await asyncio.wait(
                        {wait_task, stop_task}, return_when=asyncio.FIRST_COMPLETED
                    )
                    for task in pending:
                        task.cancel()
                    if stop_task in done and stop.is_set():
                        break
                    self._restart_count += 1
                    self._state = "degraded"
                    self._reason = "child_exited"
                except Exception:
                    self._restart_count += 1
                    self._state = "degraded"
                    self._reason = "spawn_failed"
                finally:
                    self._child = None
                try:
                    await asyncio.wait_for(stop.wait(), timeout=backoff)
                except asyncio.TimeoutError:
                    backoff = min(backoff * 2, max_backoff)
                    continue
                break
        finally:
            await self.stop()
            self._clear_bridge_pid()
            self._state = "stopped"
            self._reason = "stopped"

    async def stop(self) -> None:
        child = self._child
        if child is None or getattr(child, "returncode", None) is not None:
            return
        child.terminate()
        try:
            await asyncio.wait_for(child.wait(), timeout=5)
        except asyncio.TimeoutError:
            if hasattr(child, "kill"):
                child.kill()
                await child.wait()

    def _write_bridge_pid(self, pid: int | None) -> None:
        if not isinstance(pid, int) or pid <= 0:
            return
        self.pid_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.pid_file.write_text(f"{pid}\n", encoding="utf-8")
        os.chmod(self.pid_file, 0o600)

    def _clear_bridge_pid(self) -> None:
        try:
            self.pid_file.unlink(missing_ok=True)
        except OSError:
            pass
