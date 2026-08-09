import asyncio
import re
from pathlib import Path
from tmuxbot.backends.base import CmdOpts
from tmuxbot.commands import capture_and_push
from tmuxbot.state import Binding


def binding() -> Binding:
    item = Binding(
        name="pi-route",
        chat_id=1,
        thread_id=None,
        tmux_session="pi-route",
        tmux_window=0,
        tmux_pane=0,
        cwd=Path("/tmp/pi-route"),
        backend="pi",
        provider_session_id="old-session",
        last_session_id="old-session",
    )
    item.pending_session_handoff_after = 123.0
    return item


class PiBackend:
    name = "pi"

    def command_opts(self):
        return {
            "/new": CmdOpts(
                init_delay=0,
                poll=0,
                max_iters=1,
                expect_new_session=True,
                defer_new_session_persistence=True,
                done_pattern=re.compile(r"✓\s*New session started"),
                fallback_summary="✅ <b>Pi 新会话已启动</b>",
            )
        }

    def find_active_jsonl(self, _binding):
        return None

    def read_context_size(self, _path):
        return None


class Frontend:
    def __init__(self):
        self.html = []
        self.pre = []

    async def send_html(self, chat_id, thread_id, text):
        self.html.append((chat_id, thread_id, text))
        return None

    async def send_pre(self, chat_id, thread_id, text):
        self.pre.append((chat_id, thread_id, text))


def test_pi_new_does_not_require_jsonl_before_first_assistant_reply(monkeypatch):
    item = binding()
    frontend = Frontend()
    monkeypatch.setattr("tmuxbot.commands.tmux_capture", lambda *_args: "✓ New session started")

    asyncio.run(
        capture_and_push(
            frontend,
            item,
            PiBackend(),
            item.chat_id,
            item.thread_id,
            command="/new",
        )
    )

    assert frontend.html == [(1, None, "✅ <b>Pi 新会话已启动</b>")]
    assert frontend.pre == []
    assert item.pending_session_handoff_after == 123.0
    assert item.provider_session_id == "old-session"


def test_pi_new_still_warns_when_neither_screen_marker_nor_jsonl_switch_appears(
    monkeypatch,
):
    item = binding()
    frontend = Frontend()
    monkeypatch.setattr("tmuxbot.commands.tmux_capture", lambda *_args: "old session")

    asyncio.run(
        capture_and_push(
            frontend,
            item,
            PiBackend(),
            item.chat_id,
            item.thread_id,
            command="/new",
        )
    )

    assert "未确认完成" in frontend.html[0][2]
    assert item.pending_session_handoff_after is None
