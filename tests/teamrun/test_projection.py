from datetime import datetime, timezone

from tmuxbot.control_plane.repository import ControlPlaneRepository
from tmuxbot.teamrun.domain import AgentRole, TeamAgent, TeamRun, TeamRunState, TeamTask, TeamTaskState
from tmuxbot.teamrun.projection import render_latest_teamrun


NOW = datetime(2026, 7, 18, tzinfo=timezone.utc)


def test_channel_projection_renders_active_teamrun_in_human_chinese(tmp_path):
    database = tmp_path / "control.sqlite3"
    repo = ControlPlaneRepository(database)
    repo.migrate()
    run = TeamRun("run-1", "实现 <安全> 并审查", TeamRunState.RUNNING, 1, NOW, NOW)
    agents = [
        TeamAgent("run-1:coordinator", "run-1", AgentRole.COORDINATOR, "coordinator"),
        TeamAgent("run-1:implementer", "run-1", AgentRole.IMPLEMENTER, "implementer"),
        TeamAgent("run-1:reviewer", "run-1", AgentRole.REVIEWER, "reviewer"),
    ]
    tasks = [TeamTask(
        "implement", "run-1", "实现", "实现", AgentRole.IMPLEMENTER, TeamTaskState.WORKING,
        (), True, 1, 1, "run-1:implementer", NOW, NOW,
    )]
    repo.create_team_run(run, agents, tasks, event_id="create")

    rendered = render_latest_teamrun(database)

    assert "多 LLM 协作状态" in rendered
    assert "运行中" in rendered
    assert "实现 &lt;安全&gt; 并审查" in rendered
    assert "工作中" in rendered
