import asyncio

from tmuxbot.runtime.tmux_runtime import TmuxRuntime


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


async def no_sleep(_delay: float) -> None:
    await asyncio.sleep(0)


def runtime_for(fake: FakeTmux) -> TmuxRuntime:
    return TmuxRuntime(
        capture_func=fake.capture,
        pane_command_func=fake.pane_command,
        paste_func=fake.paste,
        send_key_func=fake.send_key,
        busy_detector=lambda pane: pane == "busy",
        sleep_func=no_sleep,
        poll_interval=0.01,
        wait_timeout=1.0,
    )


def test_busy_pane_waits_before_paste():
    fake = FakeTmux()
    fake.statuses = ["busy", "busy", "idle"]

    asyncio.run(runtime_for(fake).send_text("pane", "hello"))

    assert fake.operations == [
        "inspect",
        "inspect",
        "inspect",
        "paste:hello",
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
        "key:Enter",
        "inspect",
        "paste:two",
        "key:Enter",
        "inspect",
        "paste:three",
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
