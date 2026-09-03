"""Backend registry: single source of truth for the route adapters tmuxbot can serve.

``Binding.backend`` selects the adapter for one exact IM route.  Pi and OMP are
concurrent first-class route backends and must never be inferred from
credential, tmux pane name, or newest transcript.
"""

from __future__ import annotations

from tmuxbot.backends.base import Backend
from tmuxbot.backends.claude_code import ClaudeCodeBackend
from tmuxbot.backends.codex import CodexBackend
from tmuxbot.backends.omp import OmpBackend
from tmuxbot.backends.pi import PiBackend


def build_backend_pool() -> dict[str, Backend]:
    """Construct one instance per registered backend adapter.

    Returns a dict keyed by the same string used by ``Binding.backend`` so a
    route resolves through ``backends[route.backend]``.
    """
    return {
        "claude_code": ClaudeCodeBackend(),
        "codex": CodexBackend(),
        "pi": PiBackend(),
        "omp": OmpBackend(),
    }
