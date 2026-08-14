import asyncio

from tmuxbot.runtime.tmux_runtime import TmuxRuntime


class BusyAfterPasteFake:
    def __init__(self) -> None:
        self.now = 0.0
        self.draft = ""
        self.enter_count = 0
        self.submission_count = 0

    def capture(self, _target: str, _lines: int) -> str:
        if not self.draft:
            return "idle"
        # The provider looked idle before paste, then a queued tool/run starts.
        if self.now < 2.0:
            return f"Working...\nDRAFT:{self.draft}"
        return f"idle\nDRAFT:{self.draft}"

    def pane_command(self, _target: str) -> str:
        return "omp"

    async def paste(self, _target: str, text: str) -> None:
        self.draft = text

    def send_key(self, _target: str, key: str) -> None:
        assert key == "Enter"
        self.enter_count += 1
        if self.now >= 2.0 and self.draft:
            self.submission_count += 1
            self.draft = ""

    async def sleep(self, seconds: float) -> None:
        self.now += seconds


class IdleAfterPasteFake(BusyAfterPasteFake):
    def capture(self, _target: str, _lines: int) -> str:
        if not self.draft:
            return "idle"
        return f"idle\nDRAFT:{self.draft}"

    def send_key(self, _target: str, key: str) -> None:
        assert key == "Enter"
        self.enter_count += 1
        if self.now >= 0.5 and self.draft:
            self.submission_count += 1
            self.draft = ""


class BusyQueueFake(BusyAfterPasteFake):
    def capture(self, _target: str, _lines: int) -> str:
        if not self.draft:
            return "Working...\nEDITOR:"
        return f"Working...\nEDITOR:{self.draft}"

    def send_key(self, _target: str, key: str) -> None:
        assert key == "Enter"
        self.enter_count += 1
        if self.draft:
            self.submission_count += 1
            self.draft = ""


class FlappingIdleFake(BusyAfterPasteFake):
    def capture(self, _target: str, _lines: int) -> str:
        if not self.draft:
            return "idle"
        # A brief idle-looking frame appears immediately after paste, then the
        # queued provider run becomes visible. Stable gating must wait past it.
        if self.now < 0.4:
            return f"idle\nDRAFT:{self.draft}"
        if self.now < 1.2:
            return f"Working...\nDRAFT:{self.draft}"
        return f"idle\nDRAFT:{self.draft}"

    def send_key(self, _target: str, key: str) -> None:
        assert key == "Enter"
        self.enter_count += 1
        if self.now >= 1.2 and self.draft:
            self.submission_count += 1
            self.draft = ""


def runtime_for(fake):
    return TmuxRuntime(
        capture_func=fake.capture,
        pane_command_func=fake.pane_command,
        paste_func=fake.paste,
        send_key_func=fake.send_key,
        busy_detector=lambda pane: "Working..." in pane,
        sleep_func=fake.sleep,
        poll_interval=0.1,
        wait_timeout=5.0,
        post_paste_delay=0.1,
        input_reader=lambda pane: pane.partition("DRAFT:")[2] if "DRAFT:" in pane else "",
        submit_check_delay=0.1,
        post_render_delay=0.1,
        paste_render_timeout=0.5,
        submit_confirm_timeout=0.3,
        submit_transition_stability=0.1,
        retry_idle_stability=0.5,
        max_submit_attempts=3,
    )


def test_omp_busy_queue_submission_does_not_wait_for_idle():
    fake = BusyQueueFake()
    runtime = runtime_for(fake)
    runtime._input_reader = lambda pane: pane.partition("EDITOR:")[2]

    asyncio.run(
        runtime.send_text(
            "pane",
            "steer the current run",
            allow_busy_submission=True,
        )
    )

    assert fake.submission_count == 1
    assert fake.enter_count == 1
    assert fake.now < 1.0
    assert fake.draft == ""


def test_busy_race_after_paste_waits_for_idle_before_consuming_enter_attempts():
    fake = BusyAfterPasteFake()
    runtime = runtime_for(fake)

    asyncio.run(runtime.send_text("pane", "image and caption"))

    assert fake.submission_count == 1
    assert fake.enter_count == 1
    assert fake.draft == ""


def test_first_enter_waits_for_a_stable_post_paste_idle_window():
    fake = IdleAfterPasteFake()

    asyncio.run(runtime_for(fake).send_text("pane", "plain text"))

    assert fake.submission_count == 1
    assert fake.enter_count == 1
    assert fake.now >= 0.5


def test_retry_ignores_a_transient_idle_redraw_frame():
    fake = FlappingIdleFake()

    asyncio.run(runtime_for(fake).send_text("pane", "image with caption"))

    assert fake.submission_count == 1
    assert fake.enter_count == 1
    assert fake.draft == ""
