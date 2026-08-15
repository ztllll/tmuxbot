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
    thread_id: int | str | None  # Telegram topic int; Feishu thread str; None = root/DM
    tmux_session: str
    tmux_window: int
    tmux_pane: int
    cwd: Path
    backend: str = "claude_code"            # route adapter: claude_code / codex / pi
    bot_token_env: str = "TG_BOT_TOKEN"     # ★ 用哪个 token (env 变量名)
    channel: str = "telegram"               # ★ 前端渠道: telegram / feishu
    mention_required: bool | None = None      # None = inherit frontend deployment default
    admin: bool = False                       # privileged route; channel must enforce DM shape
    # 飞书 topic 的稳定根消息锚点。仅用于 reply_in_thread=True；Telegram/DM/root route 不使用。
    thread_root_message_id: str | None = None
    # provider 会话必须精确绑定到 tmux pane。last_session_id 保留作旧命令层兼容别名；
    # 新代码以 provider_session_id + transcript_path 为准。
    provider_session_id: str | None = None
    transcript_path: Path | None = None
    last_session_id: str | None = None
    # 由通道注入 /new 或 /clear 前设置；仅本进程内有效，供 transcript 选择器
    # 安全认领本次命令新建的会话，成功切换后由 jsonl tailer 清除。
    pending_session_handoff_after: float | None = None

    @property
    def tmux_target(self) -> str:
        return f"{self.tmux_session}:{self.tmux_window}.{self.tmux_pane}"


class State:
    """全局状态单例。所有可变状态都在这, 方便集中观察 / 测试 mock。"""

    def __init__(self) -> None:
        from aiogram import Bot  # 局部 import 避免顶层 aiogram 强依赖
        from tmuxbot.channel_health import ChannelHealthRegistry

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
        # 每个 route 当前 Turn 的通道无关过程投影与唯一可编辑消息引用。
        self.turn_projections: dict[str, object] = {}
        self.progress_messages: dict[str, dict] = {}
        self.progress_flushes: dict[str, asyncio.Task] = {}
        # 旧字段保留到过程投影迁移完成，兼容活动进程和历史测试夹具。
        self.tool_aggregator: dict[str, dict] = {}
        self.plan_messages: dict[str, dict] = {}
        # 正文准流式消息: binding.name → {msg_id, chat_id, content}
        self.reply_streams: dict[str, dict] = {}
        # 已提前推送的 Codex live 文本,用于跳过随后重复落盘的最终 message。
        self.live_text_recent: dict[str, list[str]] = {}
        # ensure_running 串行锁: 避免消息入口和后台巡检同时拉起同一个 pane
        self.ensure_locks: dict[str, asyncio.Lock] = {}
        # slash command transactions: binding.name -> CommandTransaction
        self.command_transactions: dict[str, object] = {}
        # Pi auto-compaction IM 状态卡：binding.name → {msg_id, chat_id, started_at, eta_seconds, ...}
        self.compaction_status: dict[str, dict] = {}
        # 通道连接健康：Telegram/飞书共用同一份运行时审计口径。
        self.channel_health = ChannelHealthRegistry()

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

    def find_by_source(
        self, chat_id: int | str, thread_id: int | str | None
    ) -> Binding | None:
        for b in self.bindings:
            if b.chat_id == chat_id and b.thread_id == thread_id:
                return b
        return None


# 模块级单例 — main 入口装配时使用
S = State()
