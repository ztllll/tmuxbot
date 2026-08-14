import asyncio
import json
import re
from pathlib import Path

from tmuxbot.backends.base import CmdOpts
from tmuxbot.commands import capture_and_push
from tmuxbot.state import Binding


def binding() -> Binding:
    item = Binding(
        name="omp-route",
        chat_id=1,
        thread_id=None,
        tmux_session="omp-route",
        tmux_window=0,
        tmux_pane=0,
        cwd=Path("/tmp/omp-route"),
        backend="omp",
        provider_session_id="old-session",
        last_session_id="old-session",
    )
    item.pending_session_handoff_after = 123.0
    return item


class OmpBackend:
    name = "omp"

    def command_opts(self):
        return {
            "/new": CmdOpts(
                init_delay=0,
                poll=0,
                max_iters=1,
                expect_new_session=True,
                defer_new_session_persistence=True,
                done_pattern=re.compile(r"[✓✔]\s*New session started"),
                fallback_summary="✅ <b>OMP 新会话已启动</b>",
            )
        }

    def find_active_jsonl(self, _binding):
        return None

    def read_context_size(self, _path):
        return None


class OmpCompactBackend:
    name = "omp"

    def __init__(self, transcript: Path):
        self.transcript = transcript

    def command_opts(self):
        return {
            "/compact": CmdOpts(
                init_delay=0,
                poll=0,
                max_iters=1,
                expect_compact_done=True,
                notice="⏳ OMP 压缩上下文中…",
                fallback_summary="✅ <b>OMP 上下文压缩已结束</b>",
                failure_summary="⚠️ <b>OMP 未生成压缩记录</b>",
            )
        }

    def find_active_jsonl(self, _binding):
        return self.transcript

    def compact_metadata_since(self, path, since_byte=0):
        from tmuxbot.backends.omp import OmpBackend

        return OmpBackend().compact_metadata_since(path, since_byte)


class OmpForkBackend:
    name = "omp"

    def __init__(self, old: Path, new: Path):
        self.old = old
        self.new = new
        self.calls = 0

    def command_opts(self):
        return {
            "/fork": CmdOpts(
                init_delay=0,
                poll=0,
                max_iters=2,
                expect_new_session=True,
                expect_session_handoff=True,
                fallback_summary="✅ <b>OMP 会话已分叉</b>",
            )
        }

    def find_active_jsonl(self, _binding):
        self.calls += 1
        return self.old if self.calls == 1 else self.new

    def session_identity(self, _binding, path):
        from tmuxbot.core.sessions import SessionIdentity

        session_id = "old-session" if path == self.old else "new-session"
        return SessionIdentity(
            provider="omp",
            session_id=session_id,
            transcript_path=str(path),
            tmux_target="omp-route:0.0",
            cwd="/tmp/omp-route",
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


def test_omp_new_accepts_actual_tui_marker_without_waiting_for_jsonl(monkeypatch):
    item = binding()
    frontend = Frontend()
    monkeypatch.setattr("tmuxbot.commands.tmux_capture", lambda *_args: "✔ New session started")

    asyncio.run(
        capture_and_push(
            frontend,
            item,
            OmpBackend(),
            item.chat_id,
            item.thread_id,
            command="/new",
        )
    )

    assert frontend.html == [(1, None, "✅ <b>OMP 新会话已启动</b>")]
    assert frontend.pre == []
    assert item.pending_session_handoff_after == 123.0
    assert item.provider_session_id == "old-session"


def test_omp_new_still_warns_when_neither_screen_marker_nor_jsonl_switch_appears(
    monkeypatch,
):
    item = binding()
    frontend = Frontend()
    monkeypatch.setattr("tmuxbot.commands.tmux_capture", lambda *_args: "old session")

    asyncio.run(
        capture_and_push(
            frontend,
            item,
            OmpBackend(),
            item.chat_id,
            item.thread_id,
            command="/new",
        )
    )

    assert "未确认完成" in frontend.html[0][2]
    assert item.pending_session_handoff_after is None


def test_omp_fork_claims_the_immediately_created_transcript(tmp_path, monkeypatch):
    old = tmp_path / "old.jsonl"
    new = tmp_path / "new.jsonl"
    old.write_text("{}\n", encoding="utf-8")
    new.write_text("{}\n", encoding="utf-8")
    item = binding()
    frontend = Frontend()
    backend = OmpForkBackend(old, new)
    monkeypatch.setattr("tmuxbot.commands.tmux_capture", lambda *_args: "Forked")

    asyncio.run(
        capture_and_push(
            frontend,
            item,
            backend,
            item.chat_id,
            item.thread_id,
            command="/fork",
        )
    )

    assert item.provider_session_id == "new-session"
    assert item.transcript_path == new
    assert item.pending_session_handoff_after is None
    assert "会话已分叉" in frontend.html[-1][2]


def test_omp_compact_requires_a_new_compaction_entry(tmp_path, monkeypatch):
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(
        json.dumps({"type": "session", "version": 3, "id": "old", "cwd": "/tmp/omp-route"}) + "\n",
        encoding="utf-8",
    )
    item = binding()
    frontend = Frontend()
    backend = OmpCompactBackend(transcript)
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

    assert frontend.html[-1][2] == "⚠️ <b>OMP 未生成压缩记录</b>"
    assert "压缩已结束" not in frontend.html[-1][2]


def test_omp_compact_accepts_a_new_compaction_entry_without_inventing_post_tokens(
    tmp_path, monkeypatch
):
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(
        json.dumps({"type": "session", "version": 3, "id": "old", "cwd": "/tmp/omp-route"}) + "\n",
        encoding="utf-8",
    )
    item = binding()
    frontend = Frontend()
    backend = OmpCompactBackend(transcript)
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

    assert frontend.html[-1][2] == "✅ <b>OMP 上下文压缩已结束</b>"
