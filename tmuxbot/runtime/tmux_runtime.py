"""Safe serialized input delivery for live CLI processes inside tmux."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Collection


class TmuxBusyTimeout(TimeoutError):
    """Raised when a pane does not become safe for input before the deadline."""


class TmuxSubmissionTimeout(TimeoutError):
    """Raised when the CLI keeps the same draft after bounded Enter retries."""


class TmuxRuntime:
    def __init__(
        self,
        *,
        capture_func: Callable[[str, int], str],
        pane_command_func: Callable[[str], str],
        paste_func: Callable[[str, str], Awaitable[None]],
        send_key_func: Callable[[str, str], None],
        busy_detector: Callable[[str], bool],
        sleep_func: Callable[[float], Awaitable[None]] = asyncio.sleep,
        poll_interval: float = 0.25,
        wait_timeout: float = 300.0,
        capture_lines: int = 15,
        post_paste_delay: float = 0.5,
        input_reader: Callable[[str], str | None] | None = None,
        submit_check_delay: float = 0.1,
        post_render_delay: float = 0.3,
        paste_render_timeout: float = 2.0,
        submit_confirm_timeout: float = 1.0,
        submit_transition_stability: float = 0.3,
        retry_idle_stability: float = 0.5,
        max_submit_attempts: int = 3,
    ) -> None:
        if post_paste_delay < 0:
            raise ValueError("post_paste_delay must be non-negative")
        if submit_check_delay <= 0:
            raise ValueError("submit_check_delay must be positive")
        if post_render_delay < 0:
            raise ValueError("post_render_delay must be non-negative")
        if paste_render_timeout < 0:
            raise ValueError("paste_render_timeout must be non-negative")
        if submit_confirm_timeout < 0:
            raise ValueError("submit_confirm_timeout must be non-negative")
        if submit_transition_stability < 0:
            raise ValueError("submit_transition_stability must be non-negative")
        if retry_idle_stability < 0:
            raise ValueError("retry_idle_stability must be non-negative")
        if max_submit_attempts < 1:
            raise ValueError("max_submit_attempts must be at least 1")
        self._capture = capture_func
        self._pane_command = pane_command_func
        self._paste = paste_func
        self._send_key = send_key_func
        self._is_busy = busy_detector
        self._sleep = sleep_func
        self.poll_interval = poll_interval
        self.wait_timeout = wait_timeout
        self.capture_lines = capture_lines
        self.post_paste_delay = post_paste_delay
        self._input_reader = input_reader
        self.submit_check_delay = submit_check_delay
        self.post_render_delay = post_render_delay
        self.paste_render_timeout = paste_render_timeout
        self.submit_confirm_timeout = submit_confirm_timeout
        self.submit_transition_stability = submit_transition_stability
        self.retry_idle_stability = retry_idle_stability
        self.max_submit_attempts = max_submit_attempts
        self._input_locks: dict[str, asyncio.Lock] = {}

    async def send_text(
        self,
        target: str,
        text: str,
        *,
        with_enter: bool = True,
        expected_commands: Collection[str] | None = None,
        verify_submission: bool = True,
        allow_busy_submission: bool = False,
    ) -> None:
        lock = self._input_locks.setdefault(target, asyncio.Lock())
        async with lock:
            ready_pane = (
                self._capture(target, self.capture_lines)
                if allow_busy_submission
                else await self._wait_until_ready(target)
            )
            if expected_commands is not None:
                command = self._pane_command(target)
                if command not in expected_commands:
                    raise RuntimeError(
                        f"tmux pane {target} foreground changed to {command!r} before input"
                    )
            baseline = self._input_reader(ready_pane) if self._input_reader else None
            await self._paste(target, text)
            if with_enter:
                if self.post_paste_delay:
                    await self._sleep(self.post_paste_delay)
                if not verify_submission or self._input_reader is None or not text:
                    self._send_key(target, "Enter")
                    return
                draft = await self._wait_for_rendered_draft(target, baseline, text)
                if draft is None:
                    self._send_key(target, "Enter")
                    return
                if self.post_render_delay:
                    await self._sleep(self.post_render_delay)
                for attempt in range(self.max_submit_attempts):
                    # The provider may become busy after the initial readiness
                    # check but before Enter (for example a queued Pi tool/run).
                    # Keep the rendered draft intact and wait; do not consume
                    # bounded Enter attempts while the TUI cannot submit it.
                    if not allow_busy_submission:
                        await self._wait_until_ready(
                            target,
                            stable_for=self.retry_idle_stability,
                        )
                    self._send_key(target, "Enter")
                    transition = await self._wait_for_submission_transition(
                        target,
                        draft,
                        busy_confirms_submission=not allow_busy_submission,
                    )
                    if transition:
                        return
                raise TmuxSubmissionTimeout(
                    f"tmux pane {target} kept the submitted draft after "
                    f"{self.max_submit_attempts} Enter attempts"
                )

    async def safe_launch(
        self,
        target: str,
        command: str,
        *,
        allowed_shells: Collection[str],
    ) -> bool:
        foreground = self._pane_command(target)
        if foreground not in allowed_shells:
            return False
        await self.send_text(
            target,
            command,
            expected_commands=allowed_shells,
            verify_submission=False,
            allow_busy_submission=True,
        )
        return True

    async def _wait_until_ready(self, target: str, *, stable_for: float = 0.0) -> str:
        elapsed = 0.0
        idle_elapsed = 0.0
        while True:
            pane = self._capture(target, self.capture_lines)
            if not self._is_busy(pane):
                if idle_elapsed >= stable_for:
                    return pane
                await self._sleep(self.poll_interval)
                elapsed += self.poll_interval
                idle_elapsed += self.poll_interval
                continue
            idle_elapsed = 0.0
            if elapsed >= self.wait_timeout:
                raise TmuxBusyTimeout(
                    f"tmux pane {target} stayed busy for {self.wait_timeout:.1f}s"
                )
            await self._sleep(self.poll_interval)
            elapsed += self.poll_interval

    async def _wait_for_rendered_draft(
        self,
        target: str,
        baseline: str | None,
        original: str,
    ) -> str | None:
        elapsed = 0.0
        while True:
            pane = self._capture(target, self.capture_lines)
            current = self._input_reader(pane) if self._input_reader else None
            if current and (
                not self._same_draft(baseline or "", current)
                or self._same_draft(original, current)
            ):
                return current
            if elapsed >= self.paste_render_timeout:
                return None
            await self._sleep(self.submit_check_delay)
            elapsed += self.submit_check_delay

    async def _wait_for_submission_transition(
        self,
        target: str,
        expected: str,
        *,
        busy_confirms_submission: bool = True,
    ) -> bool:
        elapsed = 0.0
        changed_elapsed: float | None = None
        while True:
            pane = self._capture(target, self.capture_lines)
            if self._is_busy(pane) and busy_confirms_submission:
                return True
            current = self._input_reader(pane) if self._input_reader else None
            if current is not None:
                if self._same_draft(expected, current):
                    changed_elapsed = None
                else:
                    if changed_elapsed is None:
                        changed_elapsed = 0.0
                    if changed_elapsed >= self.submit_transition_stability:
                        return True
            else:
                if changed_elapsed is None:
                    changed_elapsed = 0.0
                if changed_elapsed >= self.submit_transition_stability:
                    return True
            if elapsed >= self.submit_confirm_timeout and changed_elapsed is None:
                return False
            await self._sleep(self.submit_check_delay)
            elapsed += self.submit_check_delay
            if changed_elapsed is not None:
                changed_elapsed += self.submit_check_delay

    @staticmethod
    def _same_draft(expected: str, current: str | None) -> bool:
        if current is None:
            return False
        normalize = lambda value: "".join(value.split())
        return bool(normalize(expected)) and normalize(expected) == normalize(current)
