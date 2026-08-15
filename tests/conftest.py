import pytest


@pytest.fixture(autouse=True)
def _fast_im_progress_policy(monkeypatch):
    """Keep unit tests deterministic; production compact mode defaults to 4s."""
    monkeypatch.setenv("TMUXBOT_IM_PROGRESS_DELAY", "0")


@pytest.fixture(autouse=True)
def isolate_provider_binary_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep host deployment paths from changing test discovery and launch commands."""
    monkeypatch.delenv("CLAUDE_BIN", raising=False)
    monkeypatch.delenv("CODEX_BIN", raising=False)
    monkeypatch.delenv("PI_BIN", raising=False)
    for name in (
        "TMUXBOT_ADMIN_ENABLED",
        "TMUXBOT_ADMIN_CHANNEL",
        "TMUXBOT_ADMIN_CHAT_ID",
        "TMUXBOT_ADMIN_CREDENTIAL",
        "TMUXBOT_ADMIN_TMUX",
        "TMUXBOT_ADMIN_CLI",
        "TMUXBOT_ADMIN_CWD",
    ):
        monkeypatch.delenv(name, raising=False)
