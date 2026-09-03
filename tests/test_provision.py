from tmuxbot.provision import _tmux_session_name


def test_pi_provisioning_uses_project_name_with_pi_prefix():
    assert _tmux_session_name("didatodo", "pi") == "pi-didatodo"
    assert _tmux_session_name("网络同传系统项目", "pi") == "pi-网络同传系统项目"


def test_non_pi_provisioning_keeps_existing_backend_suffix():
    assert _tmux_session_name("didatodo", "claude_code") == "didatodo-claude"
    assert _tmux_session_name("didatodo", "codex") == "didatodo-codex"
