from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable

from tmuxbot.control_plane.repository import ControlPlaneRepository
from tmuxbot.tmux import tmux_send_text


class TmuxManagedSender:
    def __init__(
        self,
        repository: ControlPlaneRepository,
        *,
        send_text: Callable[[str, str], Awaitable[None]] = tmux_send_text,
    ) -> None:
        self.repository = repository
        self.send_text = send_text

    def is_registered(self, managed_session_id: str) -> bool:
        return self.repository.get_managed_session(managed_session_id) is not None

    def send(self, managed_session_id: str, envelope: dict[str, object]) -> None:
        managed = self.repository.get_managed_session(managed_session_id)
        if managed is None:
            raise ValueError("managed session is not registered")
        target = f"{managed.tmux_session}:{managed.tmux_window}.{managed.tmux_pane}"
        payload = json.dumps(envelope, ensure_ascii=False, sort_keys=True, indent=2)
        prompt = (
            "你正在参与 tmuxbot TeamRun。请严格按下面的结构化任务执行；"
            "不要自行接受结果，完成后必须提供文件、测试和 diff/commit 证据，等待独立 Reviewer。\n\n"
            f"```json\n{payload}\n```"
        )
        asyncio.run(self.send_text(target, prompt))

