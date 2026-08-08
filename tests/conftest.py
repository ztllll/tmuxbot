import pytest


@pytest.fixture(autouse=True)
def isolate_provider_binary_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep host deployment paths from changing test discovery and launch commands."""
    monkeypatch.delenv("CLAUDE_BIN", raising=False)
    monkeypatch.delenv("CODEX_BIN", raising=False)
