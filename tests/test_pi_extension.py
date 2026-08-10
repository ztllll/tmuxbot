from tmuxbot import pi_extension


def test_install_pi_handoff_extension_is_private_and_idempotent(tmp_path, monkeypatch):
    source = tmp_path / "source.ts"
    source.write_text("export default () => {};\n", encoding="utf-8")
    monkeypatch.setattr(pi_extension, "managed_extension_source", lambda: source)
    monkeypatch.setattr(pi_extension.Path, "home", lambda: tmp_path / "home")

    installed = pi_extension.install_pi_handoff_extension()

    assert installed.read_text(encoding="utf-8") == source.read_text(encoding="utf-8")
    assert installed.stat().st_mode & 0o777 == 0o600
    assert pi_extension.install_pi_handoff_extension() == installed
