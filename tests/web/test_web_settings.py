from pathlib import Path

from tmuxbot.web.settings import WebSettings


def test_web_settings_are_local_and_secure_by_default(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("TMUXBOT_DATA_DIR", str(tmp_path))
    for name in ("TMUXBOT_WEB_HOST", "TMUXBOT_WEB_PORT", "TMUXBOT_WEB_SECURE_COOKIE"):
        monkeypatch.delenv(name, raising=False)

    settings = WebSettings.from_env()

    assert settings.host == "127.0.0.1"
    assert settings.port == 8765
    assert settings.database_path == tmp_path / "control-plane.sqlite3"
    assert settings.secure_cookie is False


def test_web_settings_parse_explicit_remote_deployment(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("TMUXBOT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TMUXBOT_WEB_HOST", "0.0.0.0")
    monkeypatch.setenv("TMUXBOT_WEB_PORT", "9443")
    monkeypatch.setenv("TMUXBOT_WEB_SECURE_COOKIE", "true")

    settings = WebSettings.from_env()

    assert settings.host == "0.0.0.0"
    assert settings.port == 9443
    assert settings.secure_cookie is True
