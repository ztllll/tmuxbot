from pathlib import Path

import pytest

from tmuxbot.backends.claude_code import ClaudeCodeBackend
from tmuxbot.backends.codex import CodexBackend
from tmuxbot.frontends.base import BackendResolutionError, Frontend
from tmuxbot.state import Binding


class RoutedFrontend(Frontend):
    def __init__(self, *, backend=None, backends=None):
        self.backend = backend
        self.backends = backends or {}

    async def start_polling(self):
        return None

    async def stop(self):
        return None

    async def send_html(self, chat_id, thread_id, html_text):
        return None

    async def edit_html(self, chat_id, message_id, html_text):
        return None

    async def send_pre(self, chat_id, thread_id, raw_text):
        return None

    async def send_image(self, chat_id, thread_id, path, caption=None):
        return None

    async def send_file(self, chat_id, thread_id, path, caption=None):
        return None

    async def send_assistant_reply(self, binding, envelope):
        return None

    async def send_chat_action(self, chat_id, thread_id, action):
        return None


def binding(backend: str) -> Binding:
    return Binding(
        name=f"route-{backend}",
        chat_id=1,
        thread_id=None,
        tmux_session=f"route-{backend}",
        tmux_window=0,
        tmux_pane=0,
        cwd=Path("/tmp/route"),
        backend=backend,
    )


def test_frontend_resolves_backend_from_each_route():
    claude = ClaudeCodeBackend()
    codex = CodexBackend()
    frontend = RoutedFrontend(backends={"claude_code": claude, "codex": codex})

    assert frontend.backend_for(binding("claude_code")) is claude
    assert frontend.backend_for(binding("codex")) is codex


def test_frontend_keeps_single_backend_constructor_compatibility():
    claude = ClaudeCodeBackend()
    frontend = RoutedFrontend(backend=claude)

    assert frontend.backend_for(binding("claude_code")) is claude


def test_frontend_rejects_unknown_or_mismatched_route_backend():
    frontend = RoutedFrontend(backend=ClaudeCodeBackend())

    with pytest.raises(BackendResolutionError, match="route-codex"):
        frontend.backend_for(binding("codex"))
