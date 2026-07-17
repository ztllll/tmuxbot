from __future__ import annotations

import asyncio
import json
import shlex
from collections.abc import Awaitable, Callable

from tmuxbot.control_plane.repository import ControlPlaneRepository
from tmuxbot.providers.adapters import get_provider_adapter
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
        provider = self.repository.get_provider_profile(managed.provider_id)
        adapter = get_provider_adapter(provider.binary_name) if provider is not None else None
        prompt = _render_worker_prompt(envelope, payload, adapter.teamrun_instruction if adapter else None)
        asyncio.run(self.send_text(target, prompt))


def _render_worker_prompt(
    envelope: dict[str, object], payload: str, provider_instruction: str | None
) -> str:
    common = (
        "你正在参与 tmuxbot TeamRun。状态只以 Worker Protocol v1 回报为准；"
        "不要用自然语言自行宣布完成。"
    )
    provider_line = provider_instruction or "使用当前 CLI 的本地 shell 工具执行 worker 回报命令。"
    if envelope.get("kind") == "task.assignment":
        claim = _worker_prefix(envelope, idempotency_suffix="claim")
        progress = _worker_prefix(envelope, idempotency_suffix="progress-<n>")
        publish = _worker_prefix(envelope, idempotency_suffix="artifact-<n>")
        complete = _worker_prefix(envelope, idempotency_suffix="complete")
        block = _worker_prefix(envelope, idempotency_suffix="blocked")
        lifecycle = (
            f"1. 立即执行 `{claim} claim`。\n"
            f"2. 关键进度执行 `{progress} progress --percent <0-100>`。\n"
            f"3. 产生证据后执行 `{publish} publish-artifact --artifact 'kind=uri'`。\n"
            f"4. 完成后执行 `{complete} complete --artifact 'kind=uri'`；"
            "必须包含测试、文件或 commit 证据。\n"
            f"5. 被阻塞时执行 `{block} block --reason '原因'`。"
        )
    elif envelope.get("kind") == "review.requested":
        command = _worker_prefix(
            envelope, agent_key="reviewer_agent_id", idempotency_suffix="review"
        )
        lifecycle = (
            "只读审查给出的证据，不要修改共享目录。审查后必须执行：\n"
            f"`{command} review --verdict approved|rejected --notes '依据'`。"
        )
    else:
        lifecycle = "按 Protocol v1 中的身份、尝试号和幂等键回报状态。"
    return f"{common}\n{provider_line}\n\n{lifecycle}\n\n结构化信封：\n```json\n{payload}\n```"


def _worker_prefix(
    envelope: dict[str, object],
    *,
    agent_key: str = "assignee_agent_id",
    idempotency_suffix: str,
) -> str:
    required = ("run_id", "task_id", "attempt", agent_key, "idempotency_key")
    if any(key not in envelope for key in required):
        return "tmuxbot worker <Protocol-v1 fields>"
    return " ".join(
        [
            "tmuxbot worker",
            "--run", shlex.quote(str(envelope["run_id"])),
            "--task", shlex.quote(str(envelope["task_id"])),
            "--agent", shlex.quote(str(envelope[agent_key])),
            "--attempt", shlex.quote(str(envelope["attempt"])),
            "--idempotency-key",
            shlex.quote(f"{envelope['idempotency_key']}:{idempotency_suffix}"),
        ]
    )
