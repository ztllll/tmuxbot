"""tmuxbot — Telegram ↔ tmux 内 AI cli (claude_code / codex) TUI 桥接。

可插拔架构:
- backends/  AI cli 后端 (claude_code, codex)
- frontends/ IM 前端 (telegram, feishu)
- 主框架: state / utils / tmux / picker / jsonl / heartbeat / commands
"""
__version__ = "0.2.0"
