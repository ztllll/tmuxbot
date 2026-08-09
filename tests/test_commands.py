import asyncio
import json
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


class PiCompactBackend:
    name = "pi"

    def __init__(self, transcript: Path):
        self.transcript = transcript

    def command_opts(self):
        return {
            "/compact": CmdOpts(
                init_delay=0,
                poll=0,
                max_iters=1,
                expect_compact_done=True,
                notice="⏳ Pi 压缩上下文中…",
                fallback_summary="✅ <b>Pi 上下文压缩已结束</b>",
                failure_summary="⚠️ <b>Pi 未生成压缩记录</b>",
            )
        }

    def find_active_jsonl(self, _binding):
        return self.transcript

    def compact_metadata_since(self, path, since_byte=0):
        from tmuxbot.backends.pi import PiBackend

        return PiBackend().compact_metadata_since(path, since_byte)


class PiCloneBackend:
    name = "pi"

    def __init__(self, old: Path, new: Path):
        self.old = old
        self.new = new
        self.calls = 0

    def command_opts(self):
        return {
            "/clone": CmdOpts(
                init_delay=0,
                poll=0,
                max_iters=2,
                expect_new_session=True,
                expect_session_handoff=True,
                fallback_summary="✅ <b>Pi 会话已克隆</b>",
            )
        }

    def find_active_jsonl(self, _binding):
        self.calls += 1
        return self.old if self.calls == 1 else self.new

    def session_identity(self, _binding, path):
        from tmuxbot.core.sessions import SessionIdentity

        session_id = "old-session" if path == self.old else "new-session"
        return SessionIdentity(
            provider="pi",
            session_id=session_id,
            transcript_path=str(path),
            tmux_target="pi-route:0.0",
            cwd="/tmp/pi-route",
        )

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


def test_pi_clone_claims_the_immediately_created_transcript(tmp_path, monkeypatch):
    old = tmp_path / "old.jsonl"
    new = tmp_path / "new.jsonl"
    old.write_text("{}\n", encoding="utf-8")
    new.write_text("{}\n", encoding="utf-8")
    item = binding()
    frontend = Frontend()
    backend = PiCloneBackend(old, new)
    monkeypatch.setattr("tmuxbot.commands.tmux_capture", lambda *_args: "Cloned")

    asyncio.run(
        capture_and_push(
            frontend,
            item,
            backend,
            item.chat_id,
            item.thread_id,
            command="/clone",
        )
    )

    assert item.provider_session_id == "new-session"
    assert item.transcript_path == new
    assert item.pending_session_handoff_after is None
    assert "会话已克隆" in frontend.html[-1][2]


def test_pi_compact_requires_a_new_compaction_entry(tmp_path, monkeypatch):
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(
        json.dumps({"type": "session", "id": "old", "cwd": "/tmp/pi-route"}) + "\n",
        encoding="utf-8",
    )
    item = binding()
    frontend = Frontend()
    backend = PiCompactBackend(transcript)
    monkeypatch.setattr(
        "tmuxbot.commands.tmux_capture",
        lambda *_args: "Error: Compaction failed: Nothing to compact (session too small)",
    )

    asyncio.run(
        capture_and_push(
            frontend,
            item,
            backend,
            item.chat_id,
            item.thread_id,
            command="/compact",
        )
    )

    assert frontend.html[-1][2] == "⚠️ <b>Pi 未生成压缩记录</b>"
    assert "压缩已结束" not in frontend.html[-1][2]


def test_pi_compact_accepts_a_new_compaction_entry(tmp_path, monkeypatch):
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(
        json.dumps({"type": "session", "id": "old", "cwd": "/tmp/pi-route"}) + "\n",
        encoding="utf-8",
    )
    item = binding()
    frontend = Frontend()
    backend = PiCompactBackend(transcript)

    appended = False

    def capture(*_args):
        nonlocal appended
        if not appended:
            with transcript.open("a", encoding="utf-8") as stream:
                stream.write(
                    json.dumps(
                        {
                            "type": "compaction",
                            "tokensBefore": 50000,
                            "retainedTail": [],
                            "timestamp": "2026-08-09T00:00:00Z",
                        }
                    )
                    + "\n"
                )
            appended = True
        return "Compacted"

    monkeypatch.setattr("tmuxbot.commands.tmux_capture", capture)

    asyncio.run(
        capture_and_push(
            frontend,
            item,
            backend,
            item.chat_id,
            item.thread_id,
            command="/compact",
        )
    )

    assert frontend.html[-1][2].startswith("✅ <b>Pi 上下文压缩已结束</b>")
    assert "50.0k" in frontend.html[-1][2]
