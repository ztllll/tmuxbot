"""tmuxbot — Telegram/Feishu ↔ tmux interactive AI CLI bridge.

Pluggable architecture:
- backends/: interactive TUI adapters (Claude Code, Codex, Pi)
- frontends/: IM transports (Telegram, Feishu)
- exact topic routes select one tmux pane and one adapter
"""

__version__ = "0.3.0"
