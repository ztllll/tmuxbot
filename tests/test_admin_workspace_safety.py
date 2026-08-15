from tmuxbot.admin_cli import install_admin_context


def test_test_scoped_admin_context_does_not_touch_home_default(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    default_workspace = home / ".local/share/tmuxbot/admin"
    monkeypatch.setenv("HOME", str(home))

    explicit_workspace = tmp_path / "test-admin"
    bindings_file = tmp_path / "bindings.yaml"
    bindings_file.write_text("bindings: []\n", encoding="utf-8")

    install_admin_context(
        cwd=explicit_workspace,
        bindings_file=bindings_file,
        service="tmuxbot.service",
    )

    assert (explicit_workspace / "tmuxbot-admin-context.json").is_file()
    assert not default_workspace.exists()
