"""全局状态 + Binding dataclass。

`State.fire(coro)` 是统一的 bg task 入口:
- 用 set 保存强引用 (asyncio 默认弱引用 Task, 易被 GC)
- done_callback 自动从 set 移除 + log exception
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("tmuxbot")


@dataclass
class Binding:
    """一个 (IM 端点 ↔ tmux session ↔ cwd) 四元组

    chat_id 类型:
      - Telegram: int (正数 DM user_id / 负数 group chat_id)
      - 飞书:     str (oc_xxx 格式的 chat_id)
    """
    name: str
    chat_id: int | str        # Telegram: int; 飞书: str (oc_xxx)
    thread_id: int | None     # forum topic id; None = DM/普通群/forum General/飞书
    tmux_session: str
    tmux_window: int
    tmux_pane: int
    cwd: Path
    backend: str = "claude_code"            # ★ 多 backend: claude_code / codex
    bot_token_env: str = "TG_BOT_TOKEN"     # ★ 用哪个 token (env 变量名)
    channel: str = "telegram"               # ★ 前端渠道: telegram / feishu
    last_session_id: str | None = None      # 运行时学到的 jsonl 文件名
    idle_kill_seconds: int = 0             # >0 = 闲置超此秒数自动杀 claude 进程; 0 = 永不杀(默认)

    @property
    def tmux_target(self) -> str:
        return f"{self.tmux_session}:{self.tmux_window}.{self.tmux_pane}"


class State:
    """全局状态单例。所有可变状态都在这, 方便集中观察 / 测试 mock。"""

    def __init__(self) -> None:
        from aiogram import Bot  # 局部 import 避免顶层 aiogram 强依赖

        self.boss_user_id: int = 0
        self.bindings: list[Binding] = []
        self.offsets: dict[str, int] = {}
        self.bot: "Bot | None" = None
        self.setup_mode: bool = False
        # bg task 强引用集合
        self.bg_tasks: set[asyncio.Task] = set()
        # picker 提示去重: binding.name → 屏幕 hash
        self.picker_notified: dict[str, str] = {}
        # /rename 等输入名字态: binding.name → 触发时间戳(超过 120s 自动失效)
        self.pending_rename: dict[str, float] = {}
        # claude 活跃时间: binding.name → 最近"在干活"的时间戳
        self.last_active: dict[str, float] = {}
        # TUI 状态行指纹: binding.name → 上次"含时间+token"行的内容
        self.tui_fp: dict[str, str] = {}
        # 工具调用聚合器: binding.name → {msg_id, content_lines: list[str], last_ts, target}
        # tool_use/thinking 类事件累计到一条可编辑消息, text 事件触发"封闭"并发新消息
        self.tool_aggregator: dict[str, dict] = {}

    def fire(self, coro):
        """create_task + 强引用保存 + 完成时自动清理 + 异常自动 log"""
        t = asyncio.create_task(coro)
        self.bg_tasks.add(t)

        def _done(task: asyncio.Task) -> None:
            self.bg_tasks.discard(task)
            try:
                exc = task.exception()
            except asyncio.CancelledError:
                return
            if exc is not None:
                log.exception("bg task err", exc_info=exc)

        t.add_done_callback(_done)
        return t

    def find_by_source(self, chat_id: int, thread_id: int | None) -> Binding | None:
        for b in self.bindings:
            if b.chat_id == chat_id and b.thread_id == thread_id:
                return b
        return None


# 模块级单例 — main 入口装配时使用
S = State()
