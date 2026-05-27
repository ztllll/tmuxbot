#!/usr/bin/env python3
"""thin entry — 调 tmuxbot package。

旧 1911 行单文件已拆到 tmuxbot/ package (M2 可插拔重构):
- backends/  AI cli 后端 (claude_code, M3 加 codex)
- frontends/ IM 前端 (telegram, M4 加飞书)
- core: state / utils / tmux / picker / jsonl / heartbeat / commands

启动: python3 tmuxbot.py  或  python3 -m tmuxbot
"""
import asyncio

from tmuxbot.__main__ import main

if __name__ == "__main__":
    asyncio.run(main())
