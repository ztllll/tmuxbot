import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from tmuxbot.core.events import TerminalState, TerminalStatus
from tmuxbot.runtime import pi_terminal_health
from tmuxbot.state import Binding


class PiBackend:
    name = "pi"

    def __init__(self, transcript: Path, *, state=TerminalState.WORKING, label="⠋ Working..."):
        self.transcript = transcript
        self.status = TerminalStatus(state=state, label=label)

    def find_active_jsonl(self, _binding):
        return self.transcript

    def session_identity(self, binding, transcript):
        return SimpleNamespace(
            session_id=binding.provider_session_id,
            transcript_path=str(transcript),
        )

    def parse_terminal_status(self, _capture):
        return self.status


class Frontend:
    def __init__(self, binding, backend):
        self.bindings = [binding]
        self.backend = backend
        self.sent = []

    def backend_for(self, _binding):
        return self.backend

    async def send_html(self, chat_id, thread_id, body):
        self.sent.append((chat_id, thread_id, body))


def binding(tmp_path: Path) -> Binding:
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(
        f'{{"type":"session","id":"session-1","cwd":"{tmp_path}"}}\n',
        encoding="utf-8",
    )
    return Binding(
        name="pi-route",
        chat_id=123,
        thread_id=456,
        tmux_session="pi-route",
        tmux_window=0,
        tmux_pane=0,
        cwd=tmp_path,
        backend="pi",
        provider_session_id="session-1",
        transcript_path=transcript,
    )


def test_idle_working_retry_and_active_tool_are_silent(tmp_path, monkeypatch):
    route = binding(tmp_path)
    backend = PiBackend(route.transcript_path)
    frontend = Frontend(route, backend)
    state = SimpleNamespace(
        compaction_status={}, pending_session_handoff_after=None, ensure_locks={}
    )
    monkeypatch.setattr(pi_terminal_health, "tmux_has_session", lambda _session: True)
    monkeypatch.setattr(pi_terminal_health, "tmux_capture", lambda _target, _lines: "⠋ Working...\nfooter")
    monkeypatch.setattr(pi_terminal_health, "provider_tree_is_safe", lambda _target, _name: True)
    monkeypatch.setattr(pi_terminal_health, "provider_has_active_workload", lambda _target, _name: False)

    registry = {}
    asyncio.run(pi_terminal_health.audit_pi_terminals_once([frontend], state, registry))
    assert frontend.sent == []

    backend.status = TerminalStatus(state=TerminalState.IDLE, label="ready")
    asyncio.run(pi_terminal_health.audit_pi_terminals_once([frontend], state, registry))
    backend.status = TerminalStatus(state=TerminalState.WORKING, label="⠙ Retrying...")
    asyncio.run(pi_terminal_health.audit_pi_terminals_once([frontend], state, registry))
    backend.status = TerminalStatus(state=TerminalState.WORKING, label="⠹ Working...")
    monkeypatch.setattr(pi_terminal_health, "provider_has_active_workload", lambda _target, _name: True)
    asyncio.run(pi_terminal_health.audit_pi_terminals_once([frontend], state, registry))
    assert frontend.sent == []
    assert registry == {}


def test_three_unchanged_working_audits_notify_once_at_exact_endpoint(tmp_path, monkeypatch):
    route = binding(tmp_path)
    backend = PiBackend(route.transcript_path)
    frontend = Frontend(route, backend)
    state = SimpleNamespace(
        compaction_status={}, pending_session_handoff_after=None, ensure_locks={}
    )
    monkeypatch.setattr(pi_terminal_health, "tmux_has_session", lambda _session: True)
    monkeypatch.setattr(pi_terminal_health, "tmux_capture", lambda _target, _lines: "⠋ Working...\nstatic screen")
    monkeypatch.setattr(pi_terminal_health, "provider_tree_is_safe", lambda _target, _name: True)
    monkeypatch.setattr(pi_terminal_health, "provider_has_active_workload", lambda _target, _name: False)

    registry = {}
    for _ in range(4):
        asyncio.run(pi_terminal_health.audit_pi_terminals_once([frontend], state, registry))

    assert len(frontend.sent) == 1
    assert frontend.sent[0][:2] == (123, 456)
    assert "疑似失活" in frontend.sent[0][2]
    assert "pi-route:0.0" in frontend.sent[0][2]

    asyncio.run(pi_terminal_health.audit_pi_terminals_once([frontend], state, registry))
    assert len(frontend.sent) == 1


def test_transcript_or_screen_progress_resets_stall_and_allows_later_new_alert(tmp_path, monkeypatch):
    route = binding(tmp_path)
    backend = PiBackend(route.transcript_path)
    frontend = Frontend(route, backend)
    state = SimpleNamespace(
        compaction_status={}, pending_session_handoff_after=None, ensure_locks={}
    )
    capture = {"value": "⠋ Working...\nfirst"}
    monkeypatch.setattr(pi_terminal_health, "tmux_has_session", lambda _session: True)
    monkeypatch.setattr(pi_terminal_health, "tmux_capture", lambda _target, _lines: capture["value"])
    monkeypatch.setattr(pi_terminal_health, "provider_tree_is_safe", lambda _target, _name: True)
    monkeypatch.setattr(pi_terminal_health, "provider_has_active_workload", lambda _target, _name: False)

    registry = {}
    for _ in range(3):
        asyncio.run(pi_terminal_health.audit_pi_terminals_once([frontend], state, registry))
    assert frontend.sent == []
    assert registry[route.name]["stalled_samples"] == 2

    capture["value"] = "⠙ Working...\nnew tool output"
    asyncio.run(pi_terminal_health.audit_pi_terminals_once([frontend], state, registry))
    assert registry[route.name]["stalled_samples"] == 0

    for _ in range(3):
        asyncio.run(pi_terminal_health.audit_pi_terminals_once([frontend], state, registry))
    assert len(frontend.sent) == 1


def test_terminal_error_sidecar_notifies_once_without_waiting_for_stall_samples(
    tmp_path, monkeypatch
):
    route = binding(tmp_path)
    backend = PiBackend(route.transcript_path, state=TerminalState.IDLE, label="ready")
    frontend = Frontend(route, backend)
    state = SimpleNamespace(compaction_status={}, ensure_locks={})
    route.transcript_path.write_text(
        route.transcript_path.read_text(encoding="utf-8")
        + '{"type":"message","message":{"role":"assistant","content":[],"stopReason":"error","errorMessage":"503 unavailable"}}\n',
        encoding="utf-8",
    )
    health = SimpleNamespace(
        state="terminal_error",
        session_id="session-1",
        transcript_path=route.transcript_path,
        response_id="response-1",
        error_message="503 unavailable",
    )
    monkeypatch.setattr(pi_terminal_health, "tmux_has_session", lambda _session: True)
    monkeypatch.setattr(pi_terminal_health, "provider_tree_is_safe", lambda _target, _name: True)
    monkeypatch.setattr(pi_terminal_health, "read_session_health", lambda _target, _cwd: health)

    registry = {}
    asyncio.run(pi_terminal_health.audit_pi_terminals_once([frontend], state, registry))
    asyncio.run(pi_terminal_health.audit_pi_terminals_once([frontend], state, registry))

    assert len(frontend.sent) == 1
    assert frontend.sent[0][:2] == (123, 456)
    assert "停止自动恢复" in frontend.sent[0][2]
    assert "503 unavailable" in frontend.sent[0][2]


def test_aborted_terminal_error_sidecar_is_silent(tmp_path, monkeypatch):
    route = binding(tmp_path)
    message = "This operation was aborted"
    route.transcript_path.write_text(
        route.transcript_path.read_text(encoding="utf-8")
        + json.dumps(
            {
                "type": "message",
                "message": {
                    "role": "assistant",
                    "content": [],
                    "stopReason": "error",
                    "errorMessage": message,
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    backend = PiBackend(route.transcript_path, state=TerminalState.IDLE, label="ready")
    frontend = Frontend(route, backend)
    state = SimpleNamespace(compaction_status={}, ensure_locks={})
    health = SimpleNamespace(
        state="terminal_error",
        session_id="session-1",
        transcript_path=route.transcript_path,
        response_id="response-aborted",
        error_message=message,
    )
    monkeypatch.setattr(pi_terminal_health, "tmux_has_session", lambda _session: True)
    monkeypatch.setattr(pi_terminal_health, "provider_tree_is_safe", lambda _target, _name: True)
    monkeypatch.setattr(pi_terminal_health, "read_session_health", lambda _target, _cwd: health)
    monkeypatch.setattr(pi_terminal_health, "tmux_capture", lambda _target, _lines: "idle")
    monkeypatch.setattr(pi_terminal_health, "provider_has_active_workload", lambda *_args: False)

    asyncio.run(pi_terminal_health.audit_pi_terminals_once([frontend], state, {}))

    assert frontend.sent == []


def test_stale_terminal_error_is_silent_after_later_user_message(tmp_path, monkeypatch):
    route = binding(tmp_path)
    route.transcript_path.write_text(
        route.transcript_path.read_text(encoding="utf-8")
        + '{"type":"message","message":{"role":"assistant","content":[],"stopReason":"error","errorMessage":"failed"}}\n'
        + '{"type":"message","message":{"role":"user","content":[{"type":"text","text":"retry"}]}}\n',
        encoding="utf-8",
    )
    backend = PiBackend(route.transcript_path, state=TerminalState.IDLE, label="ready")
    frontend = Frontend(route, backend)
    state = SimpleNamespace(compaction_status={}, ensure_locks={})
    health = SimpleNamespace(
        state="terminal_error",
        session_id="session-1",
        transcript_path=route.transcript_path,
        response_id="response-1",
        error_message="failed",
    )
    monkeypatch.setattr(pi_terminal_health, "tmux_has_session", lambda _session: True)
    monkeypatch.setattr(pi_terminal_health, "provider_tree_is_safe", lambda _target, _name: True)
    monkeypatch.setattr(pi_terminal_health, "read_session_health", lambda _target, _cwd: health)
    monkeypatch.setattr(pi_terminal_health, "tmux_capture", lambda _target, _lines: "idle")
    monkeypatch.setattr(pi_terminal_health, "provider_has_active_workload", lambda *_args: False)

    asyncio.run(pi_terminal_health.audit_pi_terminals_once([frontend], state, {}))

    assert frontend.sent == []


def test_notification_registry_is_private_and_survives_bridge_restart(tmp_path, monkeypatch):
    path = tmp_path / "state" / "pi-terminal-health.json"
    registry = {
        "pi-route": {
            "fingerprint": "fingerprint",
            "session_id": "session-1",
            "stalled_samples": 3,
            "notified": True,
        }
    }

    pi_terminal_health.save_pi_terminal_health_registry(path, registry)

    assert pi_terminal_health.load_pi_terminal_health_registry(path) == registry
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    path.write_text("not json", encoding="utf-8")
    assert pi_terminal_health.load_pi_terminal_health_registry(path) == {}


def test_unpinned_or_unsafe_pi_route_fails_closed(tmp_path, monkeypatch):
    route = binding(tmp_path)
    route.provider_session_id = None
    backend = PiBackend(route.transcript_path)
    frontend = Frontend(route, backend)
    state = SimpleNamespace(
        compaction_status={}, pending_session_handoff_after=None, ensure_locks={}
    )
    monkeypatch.setattr(pi_terminal_health, "tmux_has_session", lambda _session: True)
    monkeypatch.setattr(pi_terminal_health, "tmux_capture", lambda _target, _lines: "⠋ Working...\nstatic")
    monkeypatch.setattr(pi_terminal_health, "provider_tree_is_safe", lambda _target, _name: False)

    registry = {}
    asyncio.run(pi_terminal_health.audit_pi_terminals_once([frontend], state, registry))
    assert registry == {}
    assert frontend.sent == []
