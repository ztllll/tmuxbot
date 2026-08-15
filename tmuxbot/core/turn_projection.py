"""Result-first projection of one provider turn into one compact progress card."""
from __future__ import annotations

import html
import re
from collections import deque
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ProgressIntent:
    """A channel-neutral request to create, update, or finalize a progress card."""

    action: str
    display_state: str
    body_html: str


class TurnProjection:
    """Compress provider progress into one bounded, editable channel projection."""

    def __init__(
        self,
        *,
        progress_delay_seconds: float = 0.0,
        update_interval_seconds: float = 2.0,
        max_steps: int = 3,
    ) -> None:
        self.progress_delay_seconds = max(0.0, progress_delay_seconds)
        self.update_interval_seconds = max(0.0, update_interval_seconds)
        self.tool_count = 0
        self.plan_count = 0
        self.lifecycle_count = 0
        self.error_count = 0
        self._seen: set[tuple[str, str]] = set()
        self._recent: deque[str] = deque(maxlen=max(1, max_steps))
        self._current = ""
        self._last_rendered = ""
        self._last_update_at: float | None = None
        self._opened_at: float | None = None

    @property
    def progress_was_published(self) -> bool:
        return bool(self._last_rendered)

    def consume(self, kind: str, body_html: str, *, now: float) -> list[ProgressIntent]:
        summary = _plan_summary(body_html) if kind == "plan" else _summary(body_html)
        key = (kind, summary)
        if not summary or key in self._seen:
            return []
        self._seen.add(key)
        if self._opened_at is None:
            self._opened_at = now
        self._current = summary
        self._recent.append(summary)
        if kind == "plan":
            self.plan_count += 1
        elif kind == "lifecycle":
            self.lifecycle_count += 1
        elif kind == "error":
            self.error_count += 1
        else:
            self.tool_count += 1

        rendered = self._render_working()
        if rendered == self._last_rendered:
            return []
        delay = self.next_update_in(now=now)
        if not self.progress_was_published and delay is not None and delay > 0:
            return []
        if (
            self._last_update_at is not None
            and now - self._last_update_at < self.update_interval_seconds
        ):
            return []
        self._last_rendered = rendered
        self._last_update_at = now
        return [ProgressIntent("upsert", self._display_state(), rendered)]

    def next_update_in(self, *, now: float) -> float | None:
        rendered = self._render_working()
        if rendered == self._last_rendered:
            return None
        if not self.progress_was_published:
            opened_at = self._opened_at if self._opened_at is not None else now
            return max(0.0, self.progress_delay_seconds - (now - opened_at))
        if self._last_update_at is None:
            return 0.0
        return max(0.0, self.update_interval_seconds - (now - self._last_update_at))

    def flush(self, *, now: float) -> list[ProgressIntent]:
        delay = self.next_update_in(now=now)
        if delay is None or delay > 0:
            return []
        rendered = self._render_working()
        self._last_rendered = rendered
        self._last_update_at = now
        return [ProgressIntent("upsert", self._display_state(), rendered)]

    def finalize(self, *, now: float) -> list[ProgressIntent]:
        del now  # The caller supplies time for a stable future persistence seam.
        if not self._seen:
            return []
        return [
            ProgressIntent(
                "finalize",
                "completed" if not self.error_count else "error",
                self._render_final(),
            )
        ]

    def close_without_result(
        self, *, now: float, display_state: str, heading: str,
    ) -> list[ProgressIntent]:
        """Finalize a waiting/failed turn that has no separate assistant result."""
        del now
        if not self._seen:
            return []
        recent = "；".join(self._recent)
        return [
            ProgressIntent(
                "finalize",
                display_state,
                (
                    f"{heading}\n"
                    f"· 统计：{self._stats()}\n"
                    f"· 最近：{html.escape(recent)}"
                ),
            )
        ]

    def _display_state(self) -> str:
        return "error" if self.error_count else "working"

    def _stats(self) -> str:
        parts: list[str] = []
        if self.tool_count:
            parts.append(f"工具 {self.tool_count}")
        if self.plan_count:
            parts.append(f"计划 {self.plan_count}")
        if self.lifecycle_count:
            parts.append(f"生命周期 {self.lifecycle_count}")
        if self.error_count:
            parts.append(f"异常 {self.error_count}")
        return " · ".join(parts)

    def _render_working(self) -> str:
        return (
            "💭 <b>工作进度</b>\n"
            f"· 当前：{html.escape(self._current)}\n"
            f"· 统计：{self._stats()}"
        )

    def _render_final(self) -> str:
        heading = "🔴 <b>过程异常</b>" if self.error_count else "✅ <b>过程摘要</b>"
        recent = "；".join(self._recent)
        return (
            f"{heading}\n"
            f"· 统计：{self._stats()}\n"
            f"· 最近：{html.escape(recent)}"
        )


_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _plan_summary(body_html: str) -> str:
    text = _summary(body_html, limit=500)
    statuses = re.findall(r"\b(?:pending|in_progress|completed)\b", text)
    if statuses:
        completed = statuses.count("completed")
        active = statuses.count("in_progress")
        return f"计划更新 {completed}/{len(statuses)} 完成" + (
            f"，{active} 项进行中" if active else ""
        )
    return "计划已更新"


def _summary(body_html: str, *, limit: int = 120) -> str:
    text = html.unescape(_TAG_RE.sub(" ", body_html))
    text = _WS_RE.sub(" ", text).strip()
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 1)].rstrip() + "…"
