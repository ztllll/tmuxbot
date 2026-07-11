import asyncio
import time

from tmuxbot.control_plane.models import ManagedSession, ProjectRecord, ProviderProfile
from tmuxbot.control_plane.repository import ControlPlaneRepository
from tmuxbot.teamrun.tmux_sender import TmuxManagedSender


def test_tmux_sender_resolves_server_managed_target(tmp_path):
    repo = ControlPlaneRepository(tmp_path / "db.sqlite3")
    repo.migrate()
    now = int(time.time())
    repo.upsert_provider_profile(ProviderProfile("p", "codex", "/bin/true", None, 1, 2, 3, now))
    repo.create_project(ProjectRecord("project", "P", "/tmp", 1, 2, 3, now))
    repo.create_managed_session(ManagedSession("session", "project", "p", "worker", "worker-tmux", 1, 2, "running", now))
    sent = []

    async def send_text(target, prompt):
        await asyncio.sleep(0)
        sent.append((target, prompt))

    sender = TmuxManagedSender(repo, send_text=send_text)
    sender.send("session", {"task_id": "task-1", "goal": "实现并测试"})

    assert sender.is_registered("session") is True
    assert sent[0][0] == "worker-tmux:1.2"
    assert '"task_id": "task-1"' in sent[0][1]
    assert "Reviewer" in sent[0][1]

