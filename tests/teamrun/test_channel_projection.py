import asyncio
from datetime import datetime, timezone
from pathlib import Path

from tmuxbot.control_plane.models import ManagedSession, RunEvent
from tmuxbot.state import Binding
from tmuxbot.teamrun.channel_projection import _project_event, _render_event
from tmuxbot.teamrun.domain import AgentRole, TeamAgent, TeamRun, TeamRunSnapshot, TeamRunState


NOW = datetime(2026, 7, 18, tzinfo=timezone.utc)


class FakeRepository:
    def __init__(self):
        self.snapshot = TeamRunSnapshot(
            TeamRun("run-1", "实现 <隔离> 协作", TeamRunState.RUNNING, 1, NOW, NOW),
            (TeamAgent("agent-1", "run-1", AgentRole.IMPLEMENTER, "session-1"),),
            (),
        )
        self.session = ManagedSession(
            "session-1", "project-1", "provider-1", "实施 CLI", "worker", 0, 0, "running", 1
        )

    def get_team_run(self, run_id):
        assert run_id == "run-1"
        return self.snapshot

    def get_managed_session(self, session_id):
        return self.session if session_id == "session-1" else None


class FakeFrontend:
    def __init__(self, bindings):
        self.bindings = bindings
        self.sent = []

    async def send_html(self, chat_id, thread_id, html_text):
        self.sent.append((chat_id, thread_id, html_text))


def binding(target: str) -> Binding:
    session, pane = target.split(":", 1)
    window, pane_id = pane.split(".", 1)
    return Binding("test", 1, None, session, int(window), int(pane_id), cwd=Path("/tmp"))


def test_projects_high_signal_event_only_to_bound_agent_channel():
    event = RunEvent(
        "event-1", "teamtask.working", "team_task", "implementation", {"run_id": "run-1"}, NOW, 2
    )
    matching = FakeFrontend([binding("worker:0.0")])
    unrelated = FakeFrontend([binding("other:0.0")])

    asyncio.run(_project_event(FakeRepository(), [matching, unrelated], event))

    assert len(matching.sent) == 1
    assert unrelated.sent == []
    assert "CLI 开始执行任务" in matching.sent[0][2]
    assert "&lt;隔离&gt;" in matching.sent[0][2]


def test_rendered_projection_never_exposes_event_payload():
    event = RunEvent(
        "event-1", "teamtask.dispatch_uncertain", "team_task", "implementation",
        {"run_id": "run-1", "secret_prompt": "do not expose"}, NOW, 2,
    )

    rendered = _render_event(event, "安全目标")

    assert "do not expose" not in rendered
    assert "人工确认" in rendered
