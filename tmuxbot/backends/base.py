"""后端抽象基类。

接入新 backend 时实现这个接口即可:
- ClaudeCodeBackend (现成)
- CodexBackend (M3)
- 未来其他 AI cli
"""
from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, TYPE_CHECKING

if TYPE_CHECKING:
    from tmuxbot.state import Binding


@dataclass
class CmdOpts:
    """slash 命令兜底参数: 解析器 + 轮询窗口 + 早退信号 + 兜底文案"""
    parser: Callable[[str], str | None] | None = None
    init_delay: float = 0.8
    poll: float = 0.5
    max_iters: int = 8         # 默认 ~5s 窗口
    lines: int = 80
    parser_can_retry: bool = False    # parser 返回 None 时是否继续等
    done_pattern: re.Pattern | None = None    # 屏幕命中即结束
    expect_new_session: bool = False  # /compact /clear 等会切 jsonl
    notice: str | None = None         # 进度提示文案
    fallback_summary: str | None = None  # 走完都没出 summary 时用


class Backend(ABC):
    """所有 AI cli 后端 (claude_code / codex / ...) 的统一接口。

    bot 主体只调这个接口, 不直接知道 backend 实现细节。
    """

    name: str = "base"
    pane_command_name: str = ""   # 检测 pane 是不是这个 backend (e.g. "claude" / "codex")
    start_cmd: str = ""           # 启动命令字符串 (会注入到 tmux)
    bot_commands: list[tuple[str, str]] = []  # (cmd, desc) for BotFather menu

    @abstractmethod
    def find_active_jsonl(self, b: "Binding") -> Path | None:
        """找该 binding 当前活跃的 jsonl 文件 (mtime 最新)"""

    @abstractmethod
    def parse_event(self, line: str) -> list[tuple[str, str]]:
        """jsonl 一行 → 0..多条事件。
        每条事件 = (kind, body)
        kind ∈ {"user", "assistant_text", "assistant_tools", "attachment"}
        body 已 HTML escape, 可直接发到 TG。
        """

    @abstractmethod
    def find_tui_activity_fp(self, pane: str) -> str | None:
        """从 TUI 屏幕底部抓「活跃状态行」指纹 (含时间+token 这种刷新字段)。
        指纹存在且变化 → claude 在干活;不存在 → idle。"""

    @abstractmethod
    async def ensure_running(self, b: "Binding") -> None:
        """如果 binding 对应的 tmux pane 没在跑这个 backend, 拉起来"""

    @abstractmethod
    def command_opts(self) -> dict[str, CmdOpts]:
        """slash 命令到 CmdOpts 的映射 (per-backend 配置)"""

    @abstractmethod
    def command_aliases(self) -> dict[str, str]:
        """bot 端命令别名 (e.g. /new → /clear)"""

    def aggregate_usage(self, jsonl_path: Path, last_n: int = 200) -> dict | None:
        """聚合 jsonl 的 token usage (可选实现, 默认 None)"""
        return None

    def read_context_size(self, jsonl_path: Path | None) -> int | None:
        """从 jsonl 最后一条带 usage 的 message 拿 context size
        (input_tokens + cache_read_input_tokens + cache_creation_input_tokens).

        用于 /compact 在压缩前后做对比 (e.g. 880k → 22k)。
        默认 None = backend 不支持, 调用方需检查 None 并跳过对比。"""
        return None
