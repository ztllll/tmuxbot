"""前端抽象基类。

接入新前端 (飞书 / Discord / ...) 时实现这个接口。
当前实现: TelegramFrontend
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, TYPE_CHECKING

from tmuxbot.core.capabilities import ChannelCapabilities
from tmuxbot.core.events import TerminalStatus

if TYPE_CHECKING:
    from tmuxbot.core.replies import ReplyEnvelope
    from tmuxbot.state import Binding


class Frontend(ABC):
    """所有 IM 前端 (Telegram / 飞书 / ...) 的统一接口"""

    name: str = "base"
    capabilities = ChannelCapabilities(name="base")

    @abstractmethod
    async def start_polling(self) -> None:
        """阻塞直到 polling 结束 (收 SIGTERM 或主动 stop)"""

    @abstractmethod
    async def stop(self) -> None:
        """主动停止 polling 并释放资源"""

    @abstractmethod
    async def send_html(
        self, chat_id: int, thread_id: int | None, html_text: str
    ) -> Any:
        """发 HTML 消息, 返回发送的消息对象 (用于后续 edit)"""

    @abstractmethod
    async def edit_html(
        self, chat_id: int, message_id: int, html_text: str
    ) -> None:
        """编辑已发送消息为新 HTML 内容 (工具调用聚合用)"""

    @abstractmethod
    async def send_pre(
        self, chat_id: int, thread_id: int | None, raw_text: str
    ) -> None:
        """发 <pre> 包裹的 raw 文本 (屏幕快照等)"""

    @abstractmethod
    async def send_image(
        self, chat_id: int | str, thread_id: int | None, path: str | Path,
        caption: str | None = None,
    ) -> Any:
        """发送本地图片文件为 IM 原生图片消息。"""

    @abstractmethod
    async def send_file(
        self, chat_id: int | str, thread_id: int | None, path: str | Path,
        caption: str | None = None,
    ) -> Any:
        """发送本地文件为 IM 原生文件消息。"""

    @abstractmethod
    async def send_assistant_reply(
        self, binding: "Binding", envelope: "ReplyEnvelope"
    ) -> Any:
        """发送渠道无关的 assistant reply，并返回可供后续编辑的消息对象。"""

    @abstractmethod
    async def send_chat_action(
        self, chat_id: int, thread_id: int | None, action: str
    ) -> None:
        """发"正在输入/上传"等状态 (typing 心跳用)"""

    async def send_interaction_card(
        self, chat_id: int, thread_id: int | None, html_text: str, binding_name: str
    ) -> Any:
        """发 TUI 交互卡。默认降级为普通 HTML, 支持按钮的前端可覆盖。"""
        return await self.send_html(chat_id, thread_id, html_text)

    async def send_status_html(
        self,
        chat_id: int,
        thread_id: int | None,
        html_text: str,
        *,
        display_state: str,
        footer: TerminalStatus | None = None,
    ) -> Any:
        """Send state-aware status content; unsupported channels render normal HTML."""
        return await self.send_html(chat_id, thread_id, html_text)

    async def send_reply_stream_start(
        self, binding: "Binding", html_text: str
    ) -> Any:
        """Start an incremental assistant reply; channels may override."""
        return await self.send_html(binding.chat_id, binding.thread_id, html_text)

    async def edit_reply_stream(
        self,
        binding: "Binding",
        message_id: int | str,
        html_text: str,
        *,
        final: bool = False,
    ) -> None:
        """Update or finalize an incremental assistant reply."""
        await self.edit_html(binding.chat_id, message_id, html_text)
