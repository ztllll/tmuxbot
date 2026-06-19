"""装配入口: 配 backends + frontend, 起 tailer + heartbeat + polling。

支持 `python -m tmuxbot` 或 `python tmuxbot.py` (thin entry 调这里)。
"""
from __future__ import annotations

import argparse
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
from tmuxbot import __version__
from tmuxbot.config import load_config
from tmuxbot.frontends.telegram import TelegramFrontend
from tmuxbot.heartbeat import HEARTBEAT_INTERVAL, heartbeat_typing_loop
from tmuxbot.jsonl import jsonl_poll_loop
from tmuxbot.lifecycle import lifecycle_watch_loop
from tmuxbot.state import S
from tmuxbot.tmux import tmux_has_session, tmux_new_session
from tmuxbot.utils import save_offsets
from tmuxbot.validation import TELEGRAM_TOKEN_BACKENDS

# 飞书前端按需 import (没装 lark-oapi 时不 crash, 只在实际使用时报错)
try:
    from tmuxbot.frontends.feishu import FeishuFrontend
    _FEISHU_AVAILABLE = True
except ImportError:
    _FEISHU_AVAILABLE = False
    FeishuFrontend = None  # type: ignore[assignment,misc]

# ★ Boss 架构原则: 一个 bot ↔ 一个 backend (CLI 类型) ↔ N 个 tmux 子线程
# 不同 backend 必须用不同 bot token, 避免协议串扰
TOKEN_TO_BACKEND = dict(TELEGRAM_TOKEN_BACKENDS)

# 路径可被 env 覆盖, 支持同机多实例 (如 claude-feishu / codex-feishu 各一进程,
# 因 lark-oapi 模块级全局 loop 不支持单进程跑多个飞书 app 的 ws client)
PROJECT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("TMUXBOT_DATA_DIR") or (PROJECT_DIR / "data"))
ENV_FILE = Path(os.getenv("TMUXBOT_ENV") or (PROJECT_DIR / ".env"))
BINDINGS_FILE = Path(os.getenv("TMUXBOT_BINDINGS") or (PROJECT_DIR / "bindings.yaml"))
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

    # ── 按 channel 分拣: telegram bindings vs feishu bindings ──
    tg_bindings = [b for b in S.bindings if b.channel != "feishu"]
    fs_bindings = [b for b in S.bindings if b.channel == "feishu"]

    # ── Telegram: 按 bot_token_env 把 binding 分组 (一组 = 一个 bot) ──
    bindings_by_token: dict[str, list] = defaultdict(list)
    for b in tg_bindings:
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
    frontends: list = []
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
            offsets_file=OFFSETS_FILE,
            project_base=os.getenv("TMUXBOT_PROJECT_BASE", os.path.expanduser("~/projects")),
            bot_token_env=token_env,
        )
        frontends.append(fe)

    # ── 飞书: 按 bot_token_env (= FEISHU_APP_ID_ENV 字段, 默认 "FEISHU") 分组 ──
    # bindings.yaml 飞书 binding 示例:
    #   channel: feishu
    #   bot_token_env: FEISHU   # 实际读 FEISHU_APP_ID / FEISHU_APP_SECRET
    #   backend: claude_code
    #
    # 多个飞书 binding 可用同一套 app_id/app_secret, 也可用不同的 (不同 bot_token_env)
    if fs_bindings:
        if not _FEISHU_AVAILABLE:
            log.error(
                "bindings.yaml 有 channel=feishu 的 binding, "
                "但 lark-oapi 未安装; 跳过所有飞书 bindings。"
                "请先安装: pip install lark-oapi"
            )
        else:
            # 按 bot_token_env 分组 (飞书 bot_token_env 作为 key 区分不同 app)
            fs_by_env: dict[str, list] = defaultdict(list)
            for b in fs_bindings:
                fs_by_env[b.bot_token_env].append(b)

            for env_key, bs in fs_by_env.items():
                # 约定: bot_token_env="FEISHU" → 读 FEISHU_APP_ID / FEISHU_APP_SECRET
                #        bot_token_env="FEISHU2" → 读 FEISHU2_APP_ID / FEISHU2_APP_SECRET
                app_id = os.getenv(f"{env_key}_APP_ID", "")
                app_secret = os.getenv(f"{env_key}_APP_SECRET", "")
                if not app_id or not app_secret:
                    log.warning(
                        f"飞书 {env_key}_APP_ID / {env_key}_APP_SECRET 未配置; "
                        f"跳过 {len(bs)} 个 bindings"
                    )
                    continue
                # boss_open_ids: env FEISHU_BOSS_OPEN_IDS 逗号分隔
                raw_oids = os.getenv(f"{env_key}_BOSS_OPEN_IDS", "")
                boss_open_ids = [x.strip() for x in raw_oids.split(",") if x.strip()]
                if not boss_open_ids:
                    log.warning(
                        f"{env_key}_BOSS_OPEN_IDS 未配置, 飞书 ACL 会拒绝所有消息"
                    )
                backend_name = bs[0].backend  # 同一 env_key 下 backend 应一致
                backend = backends_pool.get(backend_name)
                if backend is None:
                    log.error(f"飞书 binding backend={backend_name!r} 不在 backends_pool; 跳过")
                    continue
                # group_only_when_mentioned: 默认 False (claude 主对话方, 群里不需 @;
                # 由 ACL 的 open_id 白名单兜底, 只响应 Boss)。可用 env
                # {env_key}_GROUP_MENTION_ONLY=true 改回需 @ (注意 feishu.py 的 @ 检测
                # 目前比的是 app_id, 真要用需先修成 bot open_id)
                _mention_only = os.getenv(f"{env_key}_GROUP_MENTION_ONLY", "").lower() in ("1", "true", "yes")
                fe = FeishuFrontend(
                    app_id=app_id,
                    app_secret=app_secret,
                    state=S,
                    backend=backend,
                    bindings=bs,
                    boss_open_ids=boss_open_ids,
                    group_only_when_mentioned=_mention_only,
                    offsets_file=OFFSETS_FILE,
                    bindings_file=BINDINGS_FILE,
                    bot_token_env=env_key,
                    project_base=os.getenv("TMUXBOT_PROJECT_BASE", os.path.expanduser("~/projects")),
                )
                frontends.append(fe)
                log.info(
                    f"feishu frontend: app_id={app_id[:8]}… · backend={backend_name} "
                    f"· {len(bs)} bindings"
                )

    if not frontends:
        log.error("no frontends configured; check .env tokens and bindings.yaml")
        sys.exit(1)

    # S.bot 兼容旧代码 (picker 等用 S.bot 引用); 取第一个 TelegramFrontend 的 bot
    tg_frontends = [fe for fe in frontends if isinstance(fe, TelegramFrontend)]
    if tg_frontends:
        S.bot = tg_frontends[0].bot

    # log 启动信息
    for fe in frontends:
        if isinstance(fe, TelegramFrontend):
            try:
                me = await fe.bot.get_me()
                log.info(
                    f"tg bot @{me.username} (id={me.id}) starting · backend={fe.backend.name} "
                    f"· {len(fe.bindings)} bindings"
                )
            except Exception as e:
                log.warning(f"tg bot get_me err: {e}")
        else:
            log.info(
                f"{fe.name} frontend starting · backend={fe.backend.name} "
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
    S.fire(lifecycle_watch_loop(frontends, S))
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
            except asyncio.CancelledError:
                pass
            except Exception:
                log.exception("frontend polling task exited with error")
        for fe in frontends:
            try:
                await fe.stop()
            except Exception:
                pass
        save_offsets(OFFSETS_FILE, S.offsets, force=True)
        log.info("bye")


def run(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="tmuxbot",
        description="Telegram/Feishu <-> tmux AI CLI TUI bridge",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"tmuxbot {__version__}",
    )
    parser.parse_args(argv)
    asyncio.run(main())


if __name__ == "__main__":
    run()
