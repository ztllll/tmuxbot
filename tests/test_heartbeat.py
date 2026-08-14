import asyncio
from pathlib import Path
from types import SimpleNamespace

from tmuxbot.heartbeat import _sync_omp_compaction_status
from tmuxbot.state import Binding


class Backend:
    name = "omp"

    def parse_terminal_status(self, pane):
        label = "Auto-compacting..." if "compact" in pane else "ready"
        return SimpleNamespace(label=label)

    def estimated_compaction_seconds(self, binding):
        return 180

    def read_tasks(self, binding):
        return [{"id": 1, "subject": "Continue", "status": "in_progress"}]


class Frontend:
    def __init__(self):
        self.sent = []
        self.edited = []

    async def send_status_html(self, chat_id, thread_id, body, **kwargs):
        self.sent.append((chat_id, thread_id, body, kwargs))
        return SimpleNamespace(message_id="status-1")

    async def edit_html(self, chat_id, message_id, body):
        self.edited.append((chat_id, message_id, body))

    async def finalize_status_html(self, chat_id, message_id, body, *, display_state="completed"):
        self.edited.append((chat_id, message_id, body, display_state))


def binding():
    return Binding(
        name="omp-route",
        chat_id="oc_group",
        thread_id="omt_thread",
        tmux_session="omp-route",
        tmux_window=0,
        tmux_pane=0,
        cwd=Path("/tmp/omp-route"),
        backend="omp",
    )


def test_omp_compaction_status_sends_eta_and_updates_countdown():
    state = SimpleNamespace(compaction_status={})
    frontend = Frontend()
    backend = Backend()
    item = binding()

    asyncio.run(_sync_omp_compaction_status(state, frontend, item, backend, "compact", now=1000.0))
    asyncio.run(_sync_omp_compaction_status(state, frontend, item, backend, "compact", now=1016.0))

    assert "预计剩余约 <code>180s</code>" in frontend.sent[0][2]
    assert "Todos (0/1)" in frontend.sent[0][2]
    assert "预计剩余约 <code>164s</code>" in frontend.edited[0][2]


def test_omp_compaction_status_marks_stalled_when_tui_stops_without_jsonl_end():
    state = SimpleNamespace(compaction_status={})
    frontend = Frontend()
    backend = Backend()
    item = binding()

    asyncio.run(_sync_omp_compaction_status(state, frontend, item, backend, "compact", now=1000.0))
    asyncio.run(_sync_omp_compaction_status(state, frontend, item, backend, "idle", now=1010.0))
    asyncio.run(_sync_omp_compaction_status(state, frontend, item, backend, "idle", now=1019.0))

    assert state.compaction_status == {}
    assert "未观察到压缩完成记录" in frontend.edited[-1][2]
