"""Static SSH handoff notices for Pi interactions that require a real TUI."""
from __future__ import annotations

import html


def pi_ssh_interaction_notice(
    binding: object, *, interaction_label: str = "交互界面"
) -> str:
    session = str(getattr(binding, "tmux_session", ""))
    target = str(getattr(binding, "tmux_target", ""))
    return (
        "⚠️ <b>Pi 需要交互式操作</b>\n"
        f"· 已确认当前 TUI 正停留在<b>{html.escape(interaction_label)}</b>，"
        "tmuxbot 不会通过 IM 模拟按键或代替选择。\n"
        "· 请 SSH 登录对应主机后执行：\n"
        f"<pre>tmux select-window -t {html.escape(target)} &amp;&amp; "
        f"tmux attach-session -t {html.escape(session)}</pre>\n"
        f"· 目标 pane：<code>{html.escape(target)}</code>。完成交互后可继续在 IM 对话。"
    )
