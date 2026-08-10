from tmuxbot.runtime import route_health
from tmuxbot.runtime.route_health import PaneProcess, provider_session_file, provider_tree_is_safe


def test_provider_session_file_returns_unique_live_process_environment(monkeypatch, tmp_path):
    transcript = tmp_path / "live.jsonl"
    transcript.write_text("{}\n")
    environ = tmp_path / "environ"
    environ.write_bytes(f"HOME=/tmp\0PI_SESSION_FILE={transcript}\0".encode())
    process = PaneProcess(pid=123, parent_pid=1, state="Sl+", command="pi --approve")

    monkeypatch.setattr(route_health, "pane_processes", lambda _target: (process,))
    original_path = route_health.Path

    monkeypatch.setattr(
        route_health,
        "Path",
        lambda value: environ if str(value) == "/proc/123/environ" else original_path(value),
    )

    assert provider_session_file("project:0.0", "pi") == transcript


def test_provider_session_file_rejects_stopped_sibling(monkeypatch):
    live = PaneProcess(pid=123, parent_pid=1, state="Sl+", command="pi --approve")
    stopped = PaneProcess(pid=456, parent_pid=1, state="Tl", command="pi --approve --session old")
    monkeypatch.setattr(route_health, "pane_processes", lambda _target: (live, stopped))

    assert provider_session_file("project:0.0", "pi") is None


def test_provider_tree_accepts_live_wrapper_and_worker_without_session_variable(monkeypatch):
    wrapper = PaneProcess(pid=123, parent_pid=1, state="Sl+", command="pi --approve")
    worker = PaneProcess(pid=456, parent_pid=123, state="Sl+", command="pi")
    monkeypatch.setattr(route_health, "pane_processes", lambda _target: (wrapper, worker))

    assert provider_tree_is_safe("project:0.0", "pi") is True
    assert provider_session_file("project:0.0", "pi") is None
