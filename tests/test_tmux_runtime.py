import asyncio

import pytest

from tmuxbot.attachments import attachment_prompt
from tmuxbot.runtime.tmux_runtime import TmuxRuntime, TmuxSubmissionTimeout
from tmuxbot.tmux import _active_input_text, _is_tui_busy


class FakeTmux:
    def __init__(self) -> None:
        self.statuses = ["idle"]
        self.foreground = "codex"
        self.operations: list[str] = []
        self.pasted: list[str] = []

    def capture(self, _target: str, _lines: int) -> str:
        self.operations.append("inspect")
        if len(self.statuses) > 1:
            return self.statuses.pop(0)
        return self.statuses[0]

    def pane_command(self, _target: str) -> str:
        return self.foreground

    async def paste(self, _target: str, text: str) -> None:
        self.operations.append(f"paste:{text}")
        self.pasted.append(text)
        await asyncio.sleep(0)

    def send_key(self, _target: str, key: str) -> None:
        self.operations.append(f"key:{key}")


class SubmissionAwareFakeTmux:
    def __init__(
        self,
        *,
        accepts_enter_at: float,
        visible_at: float = 0.0,
        clear_delay: float = 0.0,
        rendered_draft: str | None = None,
        transient_hide_after_ignored: float = 0.0,
    ) -> None:
        self.accepts_enter_at = accepts_enter_at
        self.visible_at = visible_at
        self.clear_delay = clear_delay
        self.rendered_draft = rendered_draft
        self.transient_hide_after_ignored = transient_hide_after_ignored
        self.now = 0.0
        self.draft = ""
        self.accepted_at: float | None = None
        self.last_ignored_at: float | None = None
        self.enter_count = 0
        self.submission_count = 0

    def capture(self, _target: str, _lines: int) -> str:
        if not self.draft or self.now < self.visible_at:
            return ""
        if (
            self.last_ignored_at is not None
            and self.now < self.last_ignored_at + self.transient_hide_after_ignored
        ):
            return ""
        if self.accepted_at is not None and self.now >= self.accepted_at + self.clear_delay:
            self.draft = ""
            return ""
        return self.rendered_draft or self.draft

    def pane_command(self, _target: str) -> str:
        return "claude"

    async def paste(self, _target: str, text: str) -> None:
        self.draft = text

    def send_key(self, _target: str, key: str) -> None:
        assert key == "Enter"
        self.enter_count += 1
        if self.now >= self.accepts_enter_at and self.draft:
            self.submission_count += 1
            if self.accepted_at is None:
                self.accepted_at = self.now
            if self.clear_delay == 0:
                self.draft = ""
        else:
            self.last_ignored_at = self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds


def runtime_for(fake: FakeTmux, *, post_paste_delay: float = 0.5) -> TmuxRuntime:
    async def record_sleep(delay: float) -> None:
        fake.operations.append(f"sleep:{delay}")
        await asyncio.sleep(0)

    return TmuxRuntime(
        capture_func=fake.capture,
        pane_command_func=fake.pane_command,
        paste_func=fake.paste,
        send_key_func=fake.send_key,
        busy_detector=lambda pane: pane == "busy",
        sleep_func=record_sleep,
        poll_interval=0.01,
        wait_timeout=1.0,
        post_paste_delay=post_paste_delay,
    )


def submission_runtime_for(
    fake: SubmissionAwareFakeTmux,
    *,
    busy_detector=None,
    input_reader=None,
) -> TmuxRuntime:
    return TmuxRuntime(
        capture_func=fake.capture,
        pane_command_func=fake.pane_command,
        paste_func=fake.paste,
        send_key_func=fake.send_key,
        busy_detector=busy_detector or (lambda pane: False),
        sleep_func=fake.sleep,
        post_paste_delay=0.5,
        input_reader=input_reader or (lambda pane: pane),
        submit_check_delay=0.1,
        post_render_delay=0.3,
        paste_render_timeout=0.6,
        submit_confirm_timeout=0.4,
        submit_transition_stability=0.3,
        max_submit_attempts=3,
    )


def test_paste_settles_before_enter():
    fake = FakeTmux()

    asyncio.run(runtime_for(fake).send_text("pane", "line one\nline two"))

    assert fake.operations == [
        "inspect",
        "paste:line one\nline two",
        "sleep:0.5",
        "key:Enter",
    ]


def test_retries_enter_when_tui_keeps_the_pasted_draft():
    fake = SubmissionAwareFakeTmux(accepts_enter_at=1.3, visible_at=0.8)
    runtime = submission_runtime_for(fake)

    asyncio.run(runtime.send_text("pane", "请分析这张图片\n/tmp/example.png"))

    assert fake.submission_count == 1
    assert fake.enter_count == 1
    assert fake.draft == ""


def test_does_not_retry_after_first_enter_is_accepted():
    fake = SubmissionAwareFakeTmux(accepts_enter_at=0.0)
    runtime = submission_runtime_for(fake)

    asyncio.run(runtime.send_text("pane", "普通消息"))

    assert fake.submission_count == 1
    assert fake.enter_count == 1


def test_does_not_retry_when_cli_is_busy_even_if_composer_render_is_stale():
    fake = SubmissionAwareFakeTmux(accepts_enter_at=0.0, clear_delay=99.0)
    runtime = submission_runtime_for(
        fake,
        busy_detector=lambda pane: fake.submission_count > 0,
    )

    asyncio.run(runtime.send_text("pane", "普通消息"))

    assert fake.submission_count == 1
    assert fake.enter_count == 1


def test_does_not_retry_while_an_accepted_composer_is_redrawing():
    fake = SubmissionAwareFakeTmux(accepts_enter_at=0.0, clear_delay=0.25)
    runtime = submission_runtime_for(fake)

    asyncio.run(runtime.send_text("pane", "普通消息"))

    assert fake.submission_count == 1
    assert fake.enter_count == 1


def test_multiline_paste_uses_the_rendered_placeholder_as_draft_snapshot():
    fake = SubmissionAwareFakeTmux(
        accepts_enter_at=1.3,
        visible_at=0.8,
        rendered_draft="[Pasted text #1 +2 lines]",
    )
    runtime = submission_runtime_for(fake)

    asyncio.run(runtime.send_text("pane", "请分析这张图片\n\n@/tmp/example.png"))

    assert fake.submission_count == 1
    assert fake.enter_count == 1


def test_waits_after_draft_render_before_first_enter():
    fake = SubmissionAwareFakeTmux(accepts_enter_at=0.75, visible_at=0.5)
    runtime = submission_runtime_for(fake)

    asyncio.run(runtime.send_text("pane", "图片消息"))

    assert fake.submission_count == 1
    assert fake.enter_count == 1


def test_transient_empty_composer_does_not_false_confirm_submission():
    fake = SubmissionAwareFakeTmux(
        accepts_enter_at=1.0,
        visible_at=0.5,
        transient_hide_after_ignored=0.15,
    )
    runtime = submission_runtime_for(fake)

    asyncio.run(runtime.send_text("pane", "图片消息"))

    assert fake.submission_count == 1
    assert fake.enter_count == 1


def test_stable_unreadable_composer_after_enter_confirms_transition():
    fake = SubmissionAwareFakeTmux(accepts_enter_at=0.0)
    runtime = submission_runtime_for(
        fake,
        input_reader=lambda pane: None if fake.enter_count else pane,
    )

    asyncio.run(runtime.send_text("pane", "图片消息"))

    assert fake.submission_count == 1
    assert fake.enter_count == 1


def test_submission_retry_is_bounded():
    fake = SubmissionAwareFakeTmux(accepts_enter_at=99.0)
    runtime = submission_runtime_for(fake)

    with pytest.raises(TmuxSubmissionTimeout):
        asyncio.run(runtime.send_text("pane", "无法提交的消息"))

    assert fake.enter_count == 3
    assert fake.submission_count == 0


@pytest.mark.parametrize(
    ("pane", "expected"),
    [
        (
            """历史回复
────────────────────────────────────────
❯ 请分析图片
  @/tmp/tmuxbot-feishu/image.png
────────────────────────────────────────
  ⏵⏵ bypass permissions on
""",
            "请分析图片\n@/tmp/tmuxbot-feishu/image.png",
        ),
        (
            """历史回复
────────────────────────────────────────
❯\u00a0
────────────────────────────────────────
  ⏵⏵ bypass permissions on
""",
            "",
        ),
        (
            """• Working (2s • esc to interrupt)

› 请分析图片
  @/tmp/tmuxbot-feishu/image.png

  gpt-5.6 high · ~/project · Main
""",
            "请分析图片\n@/tmp/tmuxbot-feishu/image.png",
        ),
        (
            """历史回复
────────────────────────────────────────
分隔内容
────────────────────────────────────────

› 请分析图片
  @/tmp/tmuxbot-feishu/image.png

  gpt-5.6 high · ~/project · Main
""",
            "请分析图片\n@/tmp/tmuxbot-feishu/image.png",
        ),
        (
            """历史回复
────────────────────────────────────────
请分析图片
@/tmp/tmuxbot-feishu/image.png
────────────────────────────────────────
~/project (main)
↑12k ↓3k R8k CH66.7% 9.0%/128k (auto)       gpt-5.6-sol • high
""",
            "请分析图片\n@/tmp/tmuxbot-feishu/image.png",
        ),
        (
            """────────────────────────────────────────

────────────────────────────────────────
~/project
? /128k (auto)                               gpt-5.6-sol • high
""",
            "",
        ),
    ],
)
def test_active_input_text_reads_claude_and_codex_composers(pane, expected):
    assert _active_input_text(pane) == expected


def test_pi_working_indicator_blocks_input_until_tui_is_idle():
    assert _is_tui_busy("⠧ Working...\n~/repo\n↑10k ↓2k 8.0%/128k gpt-5.6 • high")
    assert not _is_tui_busy("~/repo\n↑10k ↓2k 8.0%/128k gpt-5.6 • high")


@pytest.mark.parametrize("backend_name", ["claude_code", "codex", "pi"])
def test_multiline_attachment_prompt_is_submitted_after_settle(tmp_path, backend_name):
    image = tmp_path / "input.png"
    image.write_bytes(b"png")
    prompt = attachment_prompt("检查图片", [image], backend_name=backend_name)
    fake = FakeTmux()

    asyncio.run(runtime_for(fake).send_text("pane", prompt))

    assert "\n" in prompt
    assert fake.operations[-2:] == ["sleep:0.5", "key:Enter"]
    assert fake.pasted == [prompt]


def test_zero_post_paste_delay_submits_immediately_after_paste():
    fake = FakeTmux()

    asyncio.run(runtime_for(fake, post_paste_delay=0).send_text("pane", "prompt"))

    assert fake.operations == ["inspect", "paste:prompt", "key:Enter"]


def test_without_enter_skips_settle_delay_and_key():
    fake = FakeTmux()

    asyncio.run(runtime_for(fake).send_text("pane", "draft", with_enter=False))

    assert fake.operations == ["inspect", "paste:draft"]


def test_negative_post_paste_delay_is_rejected():
    fake = FakeTmux()

    with pytest.raises(ValueError, match="post_paste_delay must be non-negative"):
        runtime_for(fake, post_paste_delay=-0.1)


def test_busy_pane_waits_before_paste():
    fake = FakeTmux()
    fake.statuses = ["busy", "busy", "idle"]

    asyncio.run(runtime_for(fake).send_text("pane", "hello"))

    assert fake.operations == [
        "inspect",
        "sleep:0.01",
        "inspect",
        "sleep:0.01",
        "inspect",
        "paste:hello",
        "sleep:0.5",
        "key:Enter",
    ]


def test_concurrent_messages_are_serialized():
    fake = FakeTmux()
    runtime = runtime_for(fake)

    async def run() -> None:
        await asyncio.gather(
            runtime.send_text("pane", "one"),
            runtime.send_text("pane", "two"),
            runtime.send_text("pane", "three"),
        )

    asyncio.run(run())

    assert fake.pasted == ["one", "two", "three"]
    assert fake.operations == [
        "inspect",
        "paste:one",
        "sleep:0.5",
        "key:Enter",
        "inspect",
        "paste:two",
        "sleep:0.5",
        "key:Enter",
        "inspect",
        "paste:three",
        "sleep:0.5",
        "key:Enter",
    ]


def test_unknown_foreground_process_rejects_launch():
    fake = FakeTmux()
    fake.foreground = "python3"
    runtime = runtime_for(fake)

    launched = asyncio.run(
        runtime.safe_launch(
            "pane",
            "codex --dangerously-bypass-approvals-and-sandbox",
            allowed_shells={"bash", "zsh"},
        )
    )

    assert not launched
    assert fake.pasted == []


def test_launch_from_shell_uses_the_same_safe_queue():
    fake = FakeTmux()
    fake.foreground = "bash"
    runtime = runtime_for(fake)

    launched = asyncio.run(
        runtime.safe_launch(
            "pane",
            "codex --dangerously-bypass-approvals-and-sandbox",
            allowed_shells={"bash", "zsh"},
        )
    )

    assert launched
    assert fake.pasted == ["codex --dangerously-bypass-approvals-and-sandbox"]


def test_launch_from_shell_does_not_require_a_tui_composer():
    fake = FakeTmux()

    async def record_sleep(delay: float) -> None:
        fake.operations.append(f"sleep:{delay}")

    runtime = TmuxRuntime(
        capture_func=fake.capture,
        pane_command_func=fake.pane_command,
        paste_func=fake.paste,
        send_key_func=fake.send_key,
        busy_detector=lambda pane: False,
        sleep_func=record_sleep,
        input_reader=lambda pane: None,
    )
    fake.foreground = "bash"

    launched = asyncio.run(
        runtime.safe_launch("pane", "codex", allowed_shells={"bash", "zsh"})
    )

    assert launched
    assert fake.operations == ["inspect", "paste:codex", "sleep:0.5", "key:Enter"]


def test_unreadable_tui_composer_falls_back_to_one_enter():
    fake = FakeTmux()

    async def record_sleep(delay: float) -> None:
        fake.operations.append(f"sleep:{delay}")

    runtime = TmuxRuntime(
        capture_func=fake.capture,
        pane_command_func=fake.pane_command,
        paste_func=fake.paste,
        send_key_func=fake.send_key,
        busy_detector=lambda pane: False,
        sleep_func=record_sleep,
        input_reader=lambda pane: None,
        submit_check_delay=0.1,
        paste_render_timeout=0.2,
    )

    asyncio.run(runtime.send_text("pane", "仍需兼容投递"))

    assert fake.operations.count("key:Enter") == 1
