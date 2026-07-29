from types import SimpleNamespace

from tmuxbot import tmux


def test_tmux_kill_session_treats_an_absent_session_as_stopped(monkeypatch):
    monkeypatch.setattr(
        tmux,
        "_tmux",
        lambda *_args: SimpleNamespace(
            returncode=1,
            stdout="",
            stderr="can't find session: expired",
        ),
    )

    assert tmux.tmux_kill_session("expired") is True


def test_tmux_kill_session_reports_real_failures(monkeypatch):
    monkeypatch.setattr(
        tmux,
        "_tmux",
        lambda *_args: SimpleNamespace(
            returncode=1,
            stdout="",
            stderr="permission denied",
        ),
    )

    assert tmux.tmux_kill_session("protected") is False


def test_tmux_kill_session_rejects_no_server_text_with_an_extra_failure(monkeypatch):
    monkeypatch.setattr(
        tmux,
        "_tmux",
        lambda *_args: SimpleNamespace(
            returncode=1,
            stdout="",
            stderr="no server running on /tmp/tmux.sock\npermission denied\n",
        ),
    )

    assert tmux.tmux_kill_session("protected") is False


def test_tmux_kill_session_normalizes_os_errors(monkeypatch):
    def fail(*_args):
        raise PermissionError("denied")

    monkeypatch.setattr(tmux, "_tmux", fail)

    assert tmux.tmux_kill_session("protected") is False
