"""装配入口: 配 backends + frontend, 起 tailer + heartbeat + polling。

支持 `python -m tmuxbot` 或 `python tmuxbot.py` (thin entry 调这里)。
"""
from __future__ import annotations

import asyncio
import fcntl
import logging
import os
import signal
import sys
from collections import defaultdict
from pathlib import Path

from tmuxbot.backends.claude_code import ClaudeCodeBackend
from tmuxbot.backends.codex import CodexBackend
from tmuxbot.config import load_config
from tmuxbot.frontends.telegram import TelegramFrontend
from tmuxbot.heartbeat import HEARTBEAT_INTERVAL, heartbeat_typing_loop
from tmuxbot.idle import idle_kill_loop
from tmuxbot.jsonl import jsonl_poll_loop
from tmuxbot.state import S
from tmuxbot.tmux import tmux_has_session, tmux_new_session
from tmuxbot.utils import save_offsets

# ★ Boss 架构原则: 一个 bot ↔ 一个 backend (CLI 类型) ↔ N 个 tmux 子线程
# 不同 backend 必须用不同 bot token, 避免协议串扰
TOKEN_TO_BACKEND = {
    "TG_BOT_TOKEN": "claude_code",
    "TG_CODEX_BOT_TOKEN": "codex",
}

PROJECT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_DIR / "data"
ENV_FILE = PROJECT_DIR / ".env"
BINDINGS_FILE = PROJECT_DIR / "bindings.yaml"
OFFSETS_FILE = DATA_DIR / "offsets.json"
LOCK_FILE = DATA_DIR / "tmuxbot.lock"

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("tmuxbot")


def acquire_lock() -> int:
    DATA_DIR.mkdir(exist_ok=True)
    fd = os.open(LOCK_FILE, os.O_CREAT | os.O_WRONLY, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log.error("tmuxbot already running (lock held); abort")
        sys.exit(1)
    os.ftruncate(fd, 0)
    os.write(fd, str(os.getpid()).encode())
    return fd


async def main() -> None:
    acquire_lock()
    load_config(ENV_FILE, BINDINGS_FILE, OFFSETS_FILE)

    # 装配 backend 实例池
    backends_pool = {
        "claude_code": ClaudeCodeBackend(),
        "codex": CodexBackend(),
    }

    # 按 bot_token_env 把 binding 分组 (一组 = 一个 bot)
    bindings_by_token: dict[str, list] = defaultdict(list)
    for b in S.bindings:
        bindings_by_token[b.bot_token_env].append(b)

    # 验证: 每组 binding 的 backend 必须跟 TOKEN_TO_BACKEND 映射一致
    # (Boss 原则: 1 bot ↔ 1 backend, 不能混)
    for token_env, bs in bindings_by_token.items():
        expected = TOKEN_TO_BACKEND.get(token_env)
        if expected is None:
            log.error(f"unknown {token_env}, skipping {len(bs)} bindings (add to TOKEN_TO_BACKEND)")
            continue
        for b in bs:
            if b.backend != expected:
                log.warning(
                    f"[{b.name}] backend={b.backend!r} != frontend backend {expected!r}; "
                    f"强制对齐到 {expected!r}"
                )
                b.backend = expected

    # 为每个 token 创建一个 TelegramFrontend
    frontends: list[TelegramFrontend] = []
    for token_env, bs in bindings_by_token.items():
        token = os.getenv(token_env)
        if not token or ":" not in token:
            log.warning(f"{token_env} missing/invalid; skipping {len(bs)} bindings")
            continue
        backend_name = TOKEN_TO_BACKEND.get(token_env)
        if backend_name is None:
            continue
        backend = backends_pool[backend_name]
        fe = TelegramFrontend(
            token=token, state=S, backend=backend, bindings=bs,
            env_file=ENV_FILE, bindings_file=BINDINGS_FILE,
        )
        frontends.append(fe)

    if not frontends:
        log.error("no frontends configured; check .env tokens and bindings.yaml")
        sys.exit(1)

    # S.bot 兼容旧代码 (picker 等用 S.bot 引用); 取第一个 frontend 的 bot
    S.bot = frontends[0].bot

    # log 启动信息
    for fe in frontends:
        me = await fe.bot.get_me()
        log.info(
            f"bot @{me.username} (id={me.id}) starting · backend={fe.backend.name} "
            f"· {len(fe.bindings)} bindings"
        )
    log.info(
        f"BOSS_USER_ID={S.boss_user_id} "
        f"({'SETUP MODE — 第一条 DM 自动锁定' if S.setup_mode else 'STRICT'})"
    )
    for b in S.bindings:
        log.info(
            f"  - {b.name}: src=({b.chat_id},{b.thread_id}) "
            f"bot_token_env={b.bot_token_env} backend={b.backend} "
            f"tmux={b.tmux_target} cwd={b.cwd}"
        )

    # 启动每个 binding 的 jsonl tailer + 每个 frontend 一个 heartbeat
    for fe in frontends:
        for b in fe.bindings:
            if not tmux_has_session(b.tmux_session):
                log.warning(f"[{b.name}] tmux session not found, creating")
                tmux_new_session(b.tmux_session, b.cwd)
            S.fire(jsonl_poll_loop(b, fe.backend, fe, S, OFFSETS_FILE))
        S.fire(heartbeat_typing_loop(S, fe))
        S.fire(idle_kill_loop(S, fe))
    log.info(
        f"{len(frontends)} frontend(s) ready · heartbeat every {HEARTBEAT_INTERVAL}s"
    )

    # graceful shutdown
    stop = asyncio.Event()

    def handler() -> None:
        log.info("received stop signal")
        stop.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, handler)
        except Exception:
            pass

    # 多个 frontend 并发 polling
    polling_tasks = [asyncio.create_task(fe.start_polling()) for fe in frontends]
    try:
        wait_stop = asyncio.create_task(stop.wait())
        done, _ = await asyncio.wait(
            {*polling_tasks, wait_stop}, return_when=asyncio.FIRST_COMPLETED
        )
    finally:
        for t in polling_tasks:
            t.cancel()
        for t in polling_tasks:
            try:
                await t
            except Exception:
                pass
        for fe in frontends:
            try:
                await fe.stop()
            except Exception:
                pass
        save_offsets(OFFSETS_FILE, S.offsets, force=True)
        log.info("bye")


if __name__ == "__main__":
    asyncio.run(main())
