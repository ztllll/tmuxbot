import asyncio
import errno
import os

import pytest

from tmuxbot.web.terminal import (
    TERMINAL_TICKET_TTL_SECONDS,
    PtyTerminal,
    TerminalTicketStore,
    TerminalService,
    TakeoverRegistry,
)


def test_terminal_ticket_is_single_use_short_lived_and_bound_to_web_session():
    store = TerminalTicketStore()
    ticket = store.issue(
        web_session_token="session-a",
        managed_session_id="managed-1",
        target="alpha:0.0",
        now=1_000,
    )

    assert len(ticket.token) >= 32
    assert ticket.expires_at == 1_000 + TERMINAL_TICKET_TTL_SECONDS
    assert store.consume(
        ticket.token, web_session_token="session-b", now=1_001
    ) is None

    consumed = store.consume(
        ticket.token, web_session_token="session-a", now=1_001
    )

    assert consumed is not None
    assert consumed.managed_session_id == "managed-1"
    assert consumed.target == "alpha:0.0"
    assert store.consume(
        ticket.token, web_session_token="session-a", now=1_001
    ) is None


def test_terminal_ticket_expires_at_deadline():
    store = TerminalTicketStore()
    ticket = store.issue(
        web_session_token="session-a",
        managed_session_id="managed-1",
        target="alpha:0.0",
        now=1_000,
    )

    assert store.consume(
        ticket.token,
        web_session_token="session-a",
        now=ticket.expires_at,
    ) is None


def test_takeover_registry_allows_only_the_owning_web_session():
    registry = TakeoverRegistry()

    assert registry.acquire("managed-1", "session-a") is True
    assert registry.acquire("managed-1", "session-a") is False
    assert registry.acquire("managed-1", "session-b") is False
    assert registry.can_input("managed-1", "session-a") is True
    assert registry.can_input("managed-1", "session-b") is False
    assert registry.release("managed-1", "session-b") is False
    assert registry.release("managed-1", "session-a") is True
    assert registry.can_input("managed-1", "session-a") is False


def test_pty_terminal_uses_fixed_tmux_attach_argv_without_shell(monkeypatch):
    master_fd, slave_fd = os.openpty()
    calls = []

    class FakeProcess:
        def __init__(self):
            self.returncode = None
            self.pid = 4321

        def poll(self):
            return self.returncode

        def terminate(self):
            self.returncode = 0

        def wait(self, timeout=None):
            return self.returncode

        def kill(self):
            raise AssertionError("clean attach shutdown must not require kill")

    process = FakeProcess()
    monkeypatch.setattr(
        "tmuxbot.web.terminal.pty.openpty", lambda: (master_fd, slave_fd)
    )

    def popen(argv, **kwargs):
        calls.append((argv, kwargs))
        return process

    monkeypatch.setattr("tmuxbot.web.terminal.subprocess.Popen", popen)

    terminal = PtyTerminal.open("alpha:0.0")

    assert len(calls) == 1
    argv, kwargs = calls[0]
    assert argv == ["tmux", "attach-session", "-t", "alpha:0.0"]
    assert kwargs["stdin"] == kwargs["stdout"] == kwargs["stderr"] == slave_fd
    assert kwargs["close_fds"] is True
    assert kwargs["shell"] is False
    assert kwargs["env"]["TERM"] == "xterm-256color"
    assert "TMUX" not in kwargs["env"]
    assert "TMUX_PANE" not in kwargs["env"]

    asyncio.run(terminal.close())
    assert process.returncode == 0


def test_pty_terminal_treats_linux_eio_as_terminal_eof(monkeypatch):
    class FinishedProcess:
        returncode = 0

        def poll(self):
            return 0

    terminal = PtyTerminal(123, FinishedProcess())

    def read(_fd, _max_bytes):
        raise OSError(errno.EIO, "pty closed")

    monkeypatch.setattr("tmuxbot.web.terminal.os.read", read)
    monkeypatch.setattr("tmuxbot.web.terminal.os.close", lambda _fd: None)

    assert asyncio.run(terminal.read()) == b""


def test_failed_start_audit_does_not_leave_untracked_takeover():
    class FailingRepository:
        def append_event(self, _event):
            raise RuntimeError("audit unavailable")

    service = TerminalService(
        repository=FailingRepository(),
        target_resolver=lambda session_id: (
            "alpha:0.0" if session_id == "managed-1" else None
        ),
        allowed_origin="http://testserver",
    )
    service.connect("managed-1", "session-a")

    with pytest.raises(RuntimeError, match="audit unavailable"):
        service.start_takeover("managed-1", "session-a")

    assert service.takeovers.is_active("managed-1") is False


def test_terminal_ticket_rejects_managed_target_reassignment():
    targets = {"managed-1": "alpha:0.0"}

    class Repository:
        def append_event(self, _event):
            return True

    service = TerminalService(
        repository=Repository(),
        target_resolver=targets.get,
        allowed_origin="http://testserver",
    )
    ticket = service.issue_ticket("managed-1", "session-a", now=1_000)
    assert ticket is not None
    targets["managed-1"] = "other:0.0"

    assert service.consume_ticket(
        ticket.token, "session-a", now=1_001
    ) is None


def test_observed_target_uses_one_lock_key_and_rejects_recreated_pane():
    class Repository:
        def append_event(self, _event):
            return True

    current = {("loose:0.0", 77)}
    service = TerminalService(
        repository=Repository(),
        target_resolver=lambda _session_id: None,
        allowed_origin="http://testserver",
        observed_target_validator=lambda target, pid: (target, pid) in current,
    )
    first = service.register_observed_target("loose:0.0", pane_pid=77)
    second = service.register_observed_target("loose:0.0", pane_pid=77)
    assert first == second
    service.connect(first, "session-a")
    assert service.start_takeover(first, "session-a") == "started"
    service.connect(second, "session-b")
    assert service.start_takeover(second, "session-b") == "conflict"
    current.clear()
    assert service.resolve_target(first) is None
