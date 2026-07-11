from pathlib import Path

from fastapi.testclient import TestClient

from tmuxbot.control_plane.repository import ControlPlaneRepository
from tmuxbot.teamrun.scheduler import TeamRunScheduler
from tmuxbot.web.app import create_app
from tmuxbot.web.settings import WebSettings


PASSWORD = "correct horse battery staple"
SETUP_TOKEN = "0123456789abcdef0123456789abcdef"


class EmptyInventory:
    def list_panes(self):
        return []


class FakeTmuxSender:
    def __init__(self):
        self.calls = []

    def is_registered(self, managed_session_id):
        return managed_session_id in {
            "tmux-coordinator",
            "tmux-implementer",
            "tmux-reviewer",
        }

    def send(self, managed_session_id, envelope):
        self.calls.append((managed_session_id, envelope))


def make_client(tmp_path: Path):
    settings = WebSettings(
        host="127.0.0.1",
        port=8765,
        database_path=tmp_path / "control.sqlite3",
        secure_cookie=False,
        setup_token=SETUP_TOKEN,
    )
    repository = ControlPlaneRepository(settings.database_path)
    repository.migrate()
    sender = FakeTmuxSender()
    scheduler = TeamRunScheduler(repository, sender)
    client = TestClient(
        create_app(
            settings,
            repository,
            EmptyInventory(),
            [],
            teamrun_scheduler=scheduler,
        ),
        base_url="http://testserver",
        client=("127.0.0.1", 50000),
    )
    return client, repository, sender


def authenticate(client: TestClient) -> str:
    status = client.get("/api/auth/status")
    bootstrap = status.json()["csrf_token"]
    response = client.post(
        "/api/auth/setup",
        headers={"X-CSRF-Token": bootstrap, "X-Setup-Token": SETUP_TOKEN},
        json={"password": PASSWORD},
    )
    assert response.status_code == 201
    return response.json()["csrf_token"]


def run_payload():
    return {
        "run_id": "run-api",
        "goal": "完成 REST TeamRun 闭环",
        "idempotency_key": "create-api-1",
        "agents": [
            {"role": "coordinator", "managed_session_id": "tmux-coordinator"},
            {"role": "implementer", "managed_session_id": "tmux-implementer"},
            {"role": "reviewer", "managed_session_id": "tmux-reviewer"},
        ],
        "tasks": [
            {
                "task_id": "implement",
                "title": "实现",
                "goal": "提交实现和测试证据",
                "role": "implementer",
                "dependencies": [],
                "requires_write": True,
                "max_attempts": 2,
            }
        ],
    }


def test_teamrun_rest_requires_auth_and_csrf(tmp_path):
    client, _, _ = make_client(tmp_path)

    assert client.get("/api/team-runs").status_code == 401
    assert client.get("/api/team-runs/run-api").status_code == 401
    assert client.post("/api/team-runs", json=run_payload()).status_code == 401
    csrf = authenticate(client)
    assert client.post("/api/team-runs", json=run_payload()).status_code == 403
    assert client.post(
        "/api/team-runs", json=run_payload(), headers={"X-CSRF-Token": csrf}
    ).status_code == 201


def test_teamrun_list_survives_a_new_web_request(tmp_path):
    client, _, _ = make_client(tmp_path)
    csrf = authenticate(client)
    headers = {"X-CSRF-Token": csrf}
    assert client.post("/api/team-runs", json=run_payload(), headers=headers).status_code == 201

    response = client.get("/api/team-runs")

    assert response.status_code == 200
    assert response.json() == [
        {
            "run_id": "run-api",
            "goal": "完成 REST TeamRun 闭环",
            "state": "draft",
        }
    ]


def test_fake_scheduler_rest_e2e_requires_independent_review(tmp_path):
    client, repository, sender = make_client(tmp_path)
    csrf = authenticate(client)
    headers = {"X-CSRF-Token": csrf}
    assert client.post("/api/team-runs", json=run_payload(), headers=headers).status_code == 201

    started = client.post(
        "/api/team-runs/run-api/start",
        json={"idempotency_key": "start-api-1"},
        headers=headers,
    )
    assert started.status_code == 200
    assert started.json()["tasks"][0]["state"] == "working"
    assert sender.calls[0][0] == "tmux-implementer"
    assert "shell" not in sender.calls[0][1]

    completed = client.post(
        "/api/team-runs/run-api/tasks/implement/complete",
        json={
            "agent_id": "run-api:implementer",
            "idempotency_key": "complete-api-1",
            "artifacts": [
                {"kind": "test", "uri": "pytest://12-passed", "metadata": {"passed": 12}}
            ],
        },
        headers=headers,
    )
    assert completed.status_code == 200
    assert completed.json()["state"] == "review"
    assert client.get("/api/team-runs/run-api/artifacts").json()[0]["uri"] == "pytest://12-passed"
    assert any(
        message["kind"] == "review_requested"
        for message in client.get("/api/team-runs/run-api/mailbox").json()
    )

    self_review = client.post(
        "/api/team-runs/run-api/tasks/implement/review",
        json={
            "reviewer_agent_id": "run-api:implementer",
            "verdict": "approved",
            "notes": "self approval",
            "idempotency_key": "self-review",
        },
        headers=headers,
    )
    assert self_review.status_code == 409

    reviewed = client.post(
        "/api/team-runs/run-api/tasks/implement/review",
        json={
            "reviewer_agent_id": "run-api:reviewer",
            "verdict": "approved",
            "notes": "evidence independently verified",
            "idempotency_key": "review-api-1",
        },
        headers=headers,
    )
    assert reviewed.status_code == 200
    assert reviewed.json()["state"] == "accepted"
    assert client.get("/api/team-runs/run-api").json()["run"]["state"] == "completed"
    event_types = [event.event_type for event in repository.list_events(after_sequence=0, limit=100)]
    assert "teamtask.review_approved" in event_types


def test_pause_resume_stop_are_idempotent_rest_commands(tmp_path):
    client, _, sender = make_client(tmp_path)
    csrf = authenticate(client)
    headers = {"X-CSRF-Token": csrf}
    client.post("/api/team-runs", json=run_payload(), headers=headers)

    paused = client.post(
        "/api/team-runs/run-api/pause",
        json={"idempotency_key": "pause-api-1"},
        headers=headers,
    )
    assert paused.json()["run"]["state"] == "paused"
    assert sender.calls == []
    resumed = client.post(
        "/api/team-runs/run-api/resume",
        json={"idempotency_key": "resume-api-1"},
        headers=headers,
    )
    assert resumed.json()["tasks"][0]["state"] == "working"
    stopped = client.post(
        "/api/team-runs/run-api/stop",
        json={"idempotency_key": "stop-api-1", "reason": "operator requested"},
        headers=headers,
    )
    assert stopped.json()["run"]["state"] == "stopped"


def test_worker_can_report_blocked_through_authenticated_api(tmp_path):
    client, _, _ = make_client(tmp_path)
    csrf = authenticate(client)
    headers = {"X-CSRF-Token": csrf}
    client.post("/api/team-runs", json=run_payload(), headers=headers)
    client.post(
        "/api/team-runs/run-api/start",
        json={"idempotency_key": "start-blocked"},
        headers=headers,
    )

    response = client.post(
        "/api/team-runs/run-api/tasks/implement/blocked",
        json={
            "agent_id": "run-api:implementer",
            "reason": "operator credential required",
            "idempotency_key": "blocked-api-1",
        },
        headers=headers,
    )

    assert response.status_code == 200
    assert response.json()["state"] == "blocked"
