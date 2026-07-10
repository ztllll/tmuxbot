"""Claude Code backend: 把原 tmuxbot.py 里跟 claude 强耦合的部分挪过来。

- parse_event: jsonl 一行 → events (区分 tools / text 给聚合器用)
- find_active_jsonl: ~/.claude/projects/<encoded-cwd>/*.jsonl 取 mtime 最新
- ensure_running: 注入 `claude --dangerously-skip-permissions [--resume <id>]`
- find_tui_activity_fp: 抓 ✶ Doing… (Xm Ys · ↓ Xk tokens) 这种行
- command_opts: /context /cost /usage /status /help /compact /clear /new /resume /rename
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from tmuxbot.backends.base import Backend, CmdOpts
from tmuxbot.core.capabilities import ProviderCapabilities
from tmuxbot.core.events import TerminalState, TerminalStatus
from tmuxbot.quota import fetch_quota
from tmuxbot.tmux import tmux_has_session, tmux_new_session, tmux_pane_command, tmux_send_text
from tmuxbot.utils import encode_cwd, render_table, strip_decorations

if TYPE_CHECKING:
    from tmuxbot.state import Binding

log = logging.getLogger("tmuxbot")

CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"
TASKS_DIR = Path.home() / ".claude" / "tasks"
# 保留 start_cmd 属性给状态/调试代码读取;真正启动时用 _start_cmd() 运行时读 CLAUDE_BIN。
START_CMD = f'{os.getenv("CLAUDE_BIN", "claude")} --dangerously-skip-permissions'


def _start_cmd() -> str:
    # CLAUDE_BIN 可配绝对路径, 防 systemd/tmux shell PATH 不含 ~/.local/bin 或命中旧 npm 入口。
    return f'{os.getenv("CLAUDE_BIN", "claude")} --dangerously-skip-permissions'


# ────────── tool 中文化 + 关键参数提取 ──────────
TOOL_ZH = {
    "Read":          "📖 读取",
    "Write":         "✏️ 写入",
    "Edit":          "✂️ 编辑",
    "MultiEdit":     "✂️ 多处编辑",
    "Bash":          "💻 执行",
    "Grep":          "🔍 搜索",
    "Glob":          "📁 列举",
    "WebSearch":     "🌐 联网搜索",
    "WebFetch":      "🌐 抓取网页",
    "Task":          "🤖 派子任务",
    "TaskCreate":    "➕ 新任务",
    "TaskUpdate":    "🔄 更新任务",
    "TaskList":      "📋 任务清单",
    "TodoWrite":     "📋 写待办",
    "NotebookEdit":  "📓 改 notebook",
    "ToolSearch":    "🧰 查工具",
    "Skill":         "💡 调用 Skill",
}


def format_tool_use(name: str, inp: dict) -> str:
    """给单条 tool_use 渲染中文 + 关键参数 (已 escape)"""
    zh = TOOL_ZH.get(name, f"🛠 {name}")
    if not isinstance(inp, dict) or not inp:
        return zh
    if name in ("Read", "Write", "Glob"):
        v = inp.get("file_path") or inp.get("pattern") or inp.get("path") or ""
        return f"{zh} <code>{html.escape(str(v)[:120])}</code>"
    if name in ("Edit", "MultiEdit"):
        return f"{zh} <code>{html.escape(str(inp.get('file_path', ''))[:120])}</code>"
    if name == "Bash":
        cmd = str(inp.get("command", ""))[:150]
        return f"{zh} <code>{html.escape(cmd)}</code>"
    if name == "Grep":
        pat = str(inp.get("pattern", ""))[:80]
        path = inp.get("path", "")
        suffix = f" <i>in {html.escape(str(path)[:60])}</i>" if path else ""
        return f"{zh} <code>{html.escape(pat)}</code>{suffix}"
    if name in ("WebSearch", "WebFetch"):
        q = str(inp.get("query") or inp.get("url") or "")[:120]
        return f"{zh} <code>{html.escape(q)}</code>"
    if name == "Task":
        d = str(inp.get("description") or inp.get("subagent_type") or "")[:80]
        return f"{zh} <i>{html.escape(d)}</i>"
    if name == "TaskCreate":
        sub = str(inp.get("subject", ""))[:100]
        return f"{zh} <i>{html.escape(sub)}</i>"
    if name == "TaskUpdate":
        tid = inp.get("taskId", "?")
        s = inp.get("status", "")
        if s:
            return f"{zh} #{html.escape(str(tid))} → <b>{html.escape(str(s))}</b>"
        return f"{zh} #{html.escape(str(tid))}"
    # fallback
    k = next(iter(inp))
    v = str(inp[k])[:120]
    return f"{zh} <i>{html.escape(k)}={html.escape(v)}</i>"


# ────────── parse_* 系列 (TUI 命令输出结构化) ──────────
CAT_ZH = {
    "System prompt": "系统提示",
    "System tools":  "系统工具",
    "Memory files":  "记忆文件",
    "Skills":        "技能",
    "Messages":      "对话消息",
    "Free space":    "剩余空间",
}


def parse_context(raw: str) -> str | None:
    """/context → 标题 + render_table 卡片"""
    clean = strip_decorations(raw)
    # 屏幕滚动历史里可能堆了多个 /context 输出 (如 [1m] 重启前的旧 200k + 现在的 1m),
    # re.search 会抓最靠上=最旧的那条 → 1M 会话被误报成 200k。只截取最后一个
    # "Context Usage" 块 (最新那次) 再解析, 杜绝旧块串味。
    _last = clean.rfind("Context Usage")
    if _last != -1:
        clean = clean[_last:]
    # 用量数字单位可省 (新会话 / 刚 /compact 后 = "0/1m tokens (0%)" 纯数字)
    total_m = re.search(
        r"(\d+(?:\.\d+)?[kmKM]?)\s*/\s*(\d+[kmKM])\s*tokens?\s*\((\d+)%\)",
        clean,
    )
    if not total_m:
        return None
    used, total, pct = total_m.groups()
    # 模型名: 老格式 claude-3-5-sonnet, 新格式 claude-opus-4-7[1m]
    # 限定族名后必须 -数字, 避免误匹配 /tmp/claude-1000/ 等路径
    model_m = re.search(r"\b(?:claude-\d-\d|claude-[a-z]+-\d)[\w\-\[\]]*", clean)
    model = model_m.group(0) if model_m else "?"

    cats = []
    for en, zh in CAT_ZH.items():
        cm = re.search(
            rf"{re.escape(en)}:\s*(\S+)\s*(?:tokens?)?\s*\(([\d.]+)%\)",
            clean,
        )
        if cm:
            cats.append((zh, cm.group(1), f"{cm.group(2)}%"))

    parts = [
        f"📊 <b>上下文用量</b>  <code>{html.escape(used)}/{html.escape(total)}</code> <b>({pct}%)</b>",
        f"🧠 模型 <code>{html.escape(model)}</code>",
    ]
    if cats:
        table = render_table(["类别", "token", "占比"], [list(c) for c in cats])
        parts.append(f"<pre>{html.escape(table)}</pre>")

    mcp_n = len(re.findall(r"mcp__\S+", clean))
    mem_n = len(set(re.findall(r"~/\.claude/CLAUDE\.md", clean)))
    skill_n = len(re.findall(r"^\s*[├└]\s*\S+:\s*[\~\d]", clean, re.M))
    foot = []
    if mcp_n: foot.append(f"MCP工具×{mcp_n}")
    if mem_n: foot.append(f"记忆×{mem_n}")
    if skill_n: foot.append(f"技能×{skill_n}")
    if foot:
        parts.append("· " + " · ".join(foot))
    return "\n".join(parts)


LIMIT_ZH = {
    "current session": "🕔 5小时窗口",
    "current week (all models)": "📅 本周(所有模型)",
    "current week (sonnet only)": "📅 本周(Sonnet)",
    "current week (opus only)": "📅 本周(Opus)",
    "current week": "📅 本周",
}


def parse_cost(raw: str) -> str | None:
    """/cost /usage → Session 块 + 限制窗口 双表"""
    clean = strip_decorations(raw)
    if not clean:
        return None
    # 同 /context: 屏幕历史可能堆多份 settings 对话框, 只取最后一次开启 (Tab 栏定位),
    # 否则 re.search 抓到旧块 → 花费/用量显示陈旧值。guard: 找不到锚点就退回全文。
    _m = list(re.finditer(r"Settings\s+Status\s+Config\s+Usage\s+Stats", clean))
    if _m:
        clean = clean[_m[-1].start():]
    parts = ["💰 <b>用量与花费</b>"]

    sess_rows: list[list[str]] = []
    cost_m = re.search(r"Total\s+cost:?\s*\$\s*([\d.]+)", clean, re.I)
    if cost_m:
        sess_rows.append(["💵 累计花费", f"${cost_m.group(1)}"])
    wall_m = re.search(r"Total\s+duration\s*\(wall\):?\s*(\S+)", clean, re.I)
    if wall_m:
        sess_rows.append(["⏱ 会话时长", wall_m.group(1)])
    api_m = re.search(r"Total\s+duration\s*\(API\):?\s*(\S+)", clean, re.I)
    if api_m and api_m.group(1) != "0s":
        sess_rows.append(["🌐 API 耗时", api_m.group(1)])
    code_m = re.search(
        r"Total\s+code\s+changes:?\s*([\d,]+)\s+lines?\s+added,?\s+([\d,]+)\s+lines?\s+removed",
        clean, re.I,
    )
    if code_m:
        sess_rows.append(["📝 代码改动", f"+{code_m.group(1)} / -{code_m.group(2)} 行"])
    usage_m = re.search(
        r"Usage:?\s*([\d,]+)\s+input,?\s+([\d,]+)\s+output,?\s+([\d,]+)\s+cache\s+read,?\s+([\d,]+)\s+cache\s+write",
        clean, re.I,
    )
    if usage_m:
        sess_rows.append(["📥 输入 token", usage_m.group(1)])
        sess_rows.append(["📤 输出 token", usage_m.group(2)])
        sess_rows.append(["📦 缓存读取", usage_m.group(3)])
        sess_rows.append(["📦 缓存创建", usage_m.group(4)])
    if sess_rows:
        parts.append("📦 <b>本会话</b>")
        parts.append(f"<pre>{html.escape(render_table(['项目', '值'], sess_rows))}</pre>")

    limit_re = re.compile(
        r"(Current\s+(?:session|week(?:\s*\([^)]+\))?))"
        r"[\s\S]{0,200}?"
        r"(\d+)\s*%\s*used"
        r"[\s\S]{0,80}?"
        r"Resets?\s+([^\n]+)",
        re.I,
    )
    limit_rows: list[list[str]] = []
    seen_titles: set[str] = set()
    for m in limit_re.finditer(clean):
        title = m.group(1).strip()
        title_key = title.lower()
        if title_key in seen_titles:
            continue
        seen_titles.add(title_key)
        pct = int(m.group(2))
        reset = m.group(3).strip()[:40]
        zh = LIMIT_ZH.get(title_key, title)
        bar_used = round(pct / 10)
        bar = "█" * bar_used + "░" * (10 - bar_used)
        limit_rows.append([zh, f"{pct}%", bar, reset])

    # ★ 限制窗口优先走 OAuth API (全 5 窗口 + 精确 resets_at), 屏幕 parse 只在 API 挂时兜底
    # claude TUI /usage 屏幕只显示当前活跃/接近上限的窗口 → 5h 0% 时屏幕仅 2 行不全
    api_quota_lines: list[str] = []
    try:
        payload, fetched_at, err = fetch_quota()
        if payload:
            api_quota_lines = _fmt_quota_lines(payload, fetched_at, err)
    except Exception as e:
        log.warning(f"fetch_quota raised in parse_cost: {e}")

    if api_quota_lines:
        parts.extend(api_quota_lines)
    elif limit_rows:
        parts.append("📈 <b>限制窗口</b>")
        parts.append(f"<pre>{html.escape(render_table(['窗口', '已用', '进度', '重置时间'], limit_rows))}</pre>")
    else:
        # 旧版 /cost 兜底 (API + 屏幕 limit_rows 都没拿到)
        win_m = re.search(
            r"(\d+)\s*/\s*(\d+)\s+(?:premium\s+)?(?:message|prompt|request)s?",
            clean, re.I,
        )
        if win_m:
            used_n, lim_n = int(win_m.group(1)), int(win_m.group(2))
            bar_used = round(used_n / lim_n * 10) if lim_n else 0
            bar = "█" * bar_used + "░" * (10 - bar_used)
            parts.append("")
            parts.append(f"📦 5小时窗口 <b>{used_n}/{lim_n}</b>  <code>{bar}</code>")
        reset_m = (
            re.search(r"[Rr]esets?\s+in\s+([^\n]+?)\s*$", clean, re.M)
            or re.search(r"[Rr]esets?\s+at\s+([^\n]+?)\s*$", clean, re.M)
        )
        if reset_m:
            parts.append(f"⏰ 重置 <code>{html.escape(reset_m.group(1).strip())}</code>")

    if len(parts) == 1:
        return None
    return "\n".join(parts)


# OAuth /api/oauth/usage 返回的窗口 key → 中文标签 (key 顺序决定渲染顺序)
_QUOTA_WINDOW_LABEL = {
    "five_hour":          "🕔 5 小时",
    "seven_day":          "📅 本周 (总)",
    "seven_day_opus":     "📅 本周 Opus",
    "seven_day_sonnet":   "📅 本周 Sonnet",
    "seven_day_oauth_apps": "📅 本周 OAuth Apps",
}


def _parse_iso8601(s: str) -> float | None:
    """ISO-8601 字符串 → unix 时间戳"""
    if not isinstance(s, str) or not s:
        return None
    try:
        return datetime.fromisoformat(s).timestamp()
    except ValueError:
        return None


def _fmt_remaining(target_ts: float) -> str:
    """unix 时间戳距当下的剩余时间 → 人读格式 Xh Ym / Xd Yh"""
    delta = int(target_ts - time.time())
    if delta <= 0:
        return "已过期"
    if delta < 3600:
        return f"{delta // 60}m{delta % 60}s"
    if delta < 86400:
        h, rem = divmod(delta, 3600)
        return f"{h}h{rem // 60}m"
    d, rem = divmod(delta, 86400)
    h = rem // 3600
    return f"{d}d{h}h"


def _fmt_quota_lines(
    payload: dict | None, fetched_at: float, last_error: str | None
) -> list[str]:
    """把 /api/oauth/usage 的 payload 渲染成 /status 的配额章节 (HTML)。
    取不到 (无 OAuth 凭证 / 走中转的环境) → 返回 [] 整段省略, 不显示"无法读取"噪音,
    保证有订阅 (直连) 与无订阅 (中转) 两种环境的 /status·/cost 核心内容一致。"""
    if not payload:
        return []
    lines = ["", "🚦 <b>订阅配额</b>"]

    rows: list[list[str]] = []
    for key, label in _QUOTA_WINDOW_LABEL.items():
        win = payload.get(key)
        if not isinstance(win, dict):
            continue
        util = win.get("utilization")
        resets_at_str = win.get("resets_at")
        if util is None and not resets_at_str:
            continue
        pct = float(util) if isinstance(util, (int, float)) else 0.0
        bar_used = max(0, min(10, round(pct / 10)))
        bar = "█" * bar_used + "░" * (10 - bar_used)
        rem = "-"
        if resets_at_str:
            ts = _parse_iso8601(resets_at_str)
            if ts is not None:
                rem = _fmt_remaining(ts)
        rows.append([label, f"{pct:.0f}%", bar, rem])

    if rows:
        lines.append(
            f"<pre>{html.escape(render_table(['窗口', '已用', '进度', '还剩'], rows))}</pre>"
        )

    extra = payload.get("extra_usage") or {}
    if isinstance(extra, dict) and extra.get("is_enabled"):
        used = extra.get("used_credits")
        cap = extra.get("monthly_limit")
        cur = extra.get("currency") or "USD"
        if used is not None and cap is not None:
            lines.append(
                f"  · 额外用量: <b>{html.escape(str(used))}/{html.escape(str(cap))} {html.escape(cur)}</b>"
            )

    if fetched_at:
        age = int(time.time() - fetched_at)
        lines.append(f"  <i>数据 {age}s 前刷新</i>")
    return lines


def parse_status(raw: str) -> str | None:
    """/status → 关键 key:value 摘要 (中文化) + Anthropic 订阅配额章节"""
    clean = strip_decorations(raw)
    # 同 /context /cost: 屏幕历史可能堆多份 settings 对话框, 只取最后一次开启
    # (Tab 栏定位), 否则 KV findall 会混进旧对话框的陈旧值。guard 找不到则退回全文。
    _m = list(re.finditer(r"Settings\s+Status\s+Config\s+Usage\s+Stats", clean))
    if _m:
        clean = clean[_m[-1].start():]
    kvs = re.findall(r"^[\s│├└]*([A-Z][A-Za-z ]+):\s*(.+)$", clean, re.M)
    if not kvs:
        return None
    STATUS_ZH = {
        "Model": "模型", "Version": "版本", "Working directory": "工作目录",
        "Account": "账号", "Project": "项目", "Session": "会话",
        "Auto-update": "自动更新", "Memory": "记忆", "Permissions": "权限",
    }
    parts = ["ℹ️ <b>状态</b>"]
    for k, v in kvs[:20]:
        k = k.strip()
        zh = STATUS_ZH.get(k, k)
        v = v.strip()[:200]
        parts.append(f"  · <b>{html.escape(zh)}</b>: {html.escape(v)}")

    # 拼配额章节 (5h / 7d 等窗口 + 重置倒计时, 走 OAuth API, 30s cache)
    # 第一次会发 HTTP 请求 (≤6s 阻塞), 后续 cache 内只查 dict 不阻塞
    try:
        payload, fetched_at, err = fetch_quota()
        parts.extend(_fmt_quota_lines(payload, fetched_at, err))
    except Exception as e:  # 兜底, 别因为 quota 拉挂整个 /status
        log.warning(f"fetch_quota raised: {e}")

    return "\n".join(parts)


def parse_help(raw: str) -> str | None:
    """/help → 命令清单 (中文)"""
    clean = strip_decorations(raw)
    cmds = re.findall(r"^\s*(/[a-z][\w-]*)\s+(.+?)\s*$", clean, re.M)
    if len(cmds) < 5:
        return None
    parts = [f"📖 <b>命令清单</b> · 共 {len(cmds)} 条"]
    for name, desc in cmds[:40]:
        d = desc.strip()[:60]
        parts.append(f"  <code>{html.escape(name)}</code> — {html.escape(d)}")
    if len(cmds) > 40:
        parts.append(f"\n  …还有 {len(cmds) - 40} 个,终端查看完整")
    return "\n".join(parts)


COMPACT_DONE_RE = re.compile(r"Compacted|compact.*complete|压缩完成|context\s+compacted", re.I)


def parse_compact(raw: str) -> str | None:
    """/compact → 完成简短中文反馈 (未完成 None 让 capture_and_push 继续轮询)"""
    clean = strip_decorations(raw)
    if not COMPACT_DONE_RE.search(clean):
        return None
    parts = ["✅ <b>上下文已压缩</b>"]
    delta = re.search(
        r"(\d+(?:\.\d+)?[kmKM])\s*(?:tokens?)?\s*(?:→|->)\s*(\d+(?:\.\d+)?[kmKM])",
        clean,
    )
    if delta:
        parts.append(f"📉 token <code>{delta.group(1)}</code> → <code>{delta.group(2)}</code>")
    if re.search(r"ctrl[+\-]o.*summary", clean, re.I):
        parts.append("📜 完整摘要 TUI 内 <code>ctrl+o</code> 查看")
    return "\n".join(parts)


def parse_clear(raw: str) -> str | None:
    return "🧹 <b>会话已清空</b>\n· 新 session 已开启,历史已离线归档"


def parse_new(raw: str) -> str | None:
    return "🆕 <b>新会话已开启</b>\n· 上下文已清空,历史已离线归档"


def parse_resume(raw: str) -> str | None:
    return (
        "🔄 <b>正在打开 session 列表</b>\n"
        "· picker 出现后会自动推到 TG\n"
        "· 列表上点 1-9 选 session,⎋ 取消"
    )


def parse_rename(raw: str) -> str | None:
    return (
        "📝 <b>请发新名字</b>\n"
        "· 下一条 TG 文本作为新对话名字\n"
        "· 发 /esc 取消等待"
    )


# ────────── TUI 活跃指纹 (heartbeat 用) ──────────
# 同一行内同时含「时间」和「token 数」(claude 工作时底部 ✶ Doing… (Xm Ys · ↓ Xk tokens))
_TUI_TIME_RE = re.compile(r"\b\d+m\s+\d+s|\b\d+s\b")
_TUI_TOK_RE = re.compile(r"\b\d+(?:\.\d+)?[km]?\s*tokens?\b", re.I)


def _parse_scaled_number(raw: str) -> int:
    value = float(raw[:-1]) if raw[-1:].lower() in {"k", "m"} else float(raw)
    suffix = raw[-1:].lower()
    if suffix == "k":
        value *= 1_000
    elif suffix == "m":
        value *= 1_000_000
    return int(value)


def _parse_duration(raw: str) -> int:
    minutes = re.search(r"(\d+)m", raw)
    seconds = re.search(r"(\d+)s", raw)
    return (int(minutes.group(1)) * 60 if minutes else 0) + (
        int(seconds.group(1)) if seconds else 0
    )


# ────────── ClaudeCodeBackend ──────────
class ClaudeCodeBackend(Backend):
    name = "claude_code"
    pane_command_name = "claude"
    start_cmd = START_CMD

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            name=self.name,
            supports_hooks=True,
            supports_resume=True,
            supports_continue=True,
            supports_tasks=True,
            supports_usage=True,
            supports_interactive_pickers=True,
        )

    def is_running_command(self, command: str) -> bool:
        return command == "claude"

    def parse_terminal_status(self, pane: str) -> TerminalStatus | None:
        clean = strip_decorations(pane)
        if not clean.strip():
            return None

        working_line = next(
            (
                line.strip()
                for line in clean.splitlines()
                if _TUI_TIME_RE.search(line) and _TUI_TOK_RE.search(line)
            ),
            None,
        )
        state = TerminalState.WORKING if working_line else TerminalState.IDLE
        duration = _parse_duration(working_line) if working_line else None

        permission = None
        permission_match = re.search(
            r"\b(accept edits|bypass permissions|plan mode|manual)(?:\s+on)?\b",
            clean,
            re.I,
        )
        if permission_match:
            permission = permission_match.group(1).lower()

        context_used = context_limit = None
        context_match = re.search(
            r"(\d+(?:\.\d+)?[km]?)\s*/\s*(\d+(?:\.\d+)?[km]?)\s+tokens",
            clean,
            re.I,
        )
        if context_match:
            context_used = _parse_scaled_number(context_match.group(1))
            context_limit = _parse_scaled_number(context_match.group(2))

        model_match = re.search(r"\b(claude-[\w-]+)\b", clean, re.I)
        return TerminalStatus(
            state=state,
            label=working_line or "ready",
            model=model_match.group(1) if model_match else None,
            permission_mode=permission,
            duration_seconds=duration,
            context_used=context_used,
            context_limit=context_limit,
        )

    # 给 BotFather 注册菜单 (其他 backend 可以有不同清单)
    bot_commands = [
        ("status", "ℹ️ 综合状态(含上下文/余量/缓存)"),
        ("info", "📊 累计 token + 缓存命中率(只读 jsonl)"),
        ("whoami", "👤 我的 user_id / chat_id"),
        ("new", "🆕 开新会话(=/clear)"),
        ("resume", "🔄 恢复历史会话(弹 picker)"),
        ("esc", "⎋ 中断当前生成"),
        ("cc", "⌃C 取消/清空输入"),
        ("eof", "⌃D 退出 claude"),
        ("screen", "📷 抓 tmux 屏幕"),
        ("restart", "🔄 重启 claude"),
    ]

    def find_active_jsonl(self, b: "Binding") -> Path | None:
        d = CLAUDE_PROJECTS_DIR / encode_cwd(b.cwd)
        if not d.exists():
            return None
        if b.transcript_path:
            pinned = Path(b.transcript_path)
            try:
                in_project = pinned.parent.resolve() == d.resolve()
            except OSError:
                in_project = False
            id_matches = (
                not b.provider_session_id or pinned.stem == b.provider_session_id
            )
            if in_project and id_matches and pinned.is_file():
                return pinned
        if b.provider_session_id:
            pinned = d / f"{b.provider_session_id}.jsonl"
            if pinned.is_file():
                return pinned
        files = list(d.glob("*.jsonl"))
        if not files:
            return None
        return max(files, key=lambda p: p.stat().st_mtime)

    def read_tasks(self, b: "Binding") -> list:
        """读 harness 任务文件 ~/.claude/tasks/<session_id>/*.json → 当前任务列表。
        session_id = 该 binding active jsonl 的 stem。无目录/无文件 → []。"""
        jl = self.find_active_jsonl(b)
        if not jl:
            return []
        tdir = TASKS_DIR / jl.stem
        if not tdir.is_dir():
            return []
        tasks = []
        for f in sorted(tdir.glob("*.json")):
            try:
                d = json.loads(f.read_text())
                tasks.append({
                    "id": int(d.get("id", 0)),
                    "subject": d.get("subject", ""),
                    "status": d.get("status", ""),
                })
            except Exception:
                continue
        tasks.sort(key=lambda t: t["id"])
        return tasks

    def status_extra(self, b: "Binding") -> str:
        """给 /status 补 jsonl 来源的综合信息: 当前上下文 + 会话累计 token + 缓存命中率。
        全部读 jsonl, 跟直连/中转无关 → 两端一致 (配额另由 parse_status 走 OAuth, 中转省略)。"""
        jl = self.find_active_jsonl(b)
        if not jl:
            return ""
        lines: list[str] = []
        ctx = self.read_context_size(jl)
        if ctx:
            lines.append(f"🧮 <b>当前上下文</b> <code>{ctx / 1000:.1f}k</code> tokens")
        st = self.aggregate_usage(jl)
        if st:
            def _f(n: int) -> str:
                return f"{n:,}"
            total_in = st["input"] + st["cache_create"] + st["cache_read"]
            lines.append(f"📊 <b>会话累计</b> · 助手 {st['count']} 条")
            lines.append(
                f"  📥 计费输入 <code>{_f(total_in)}</code> "
                f"(新 {_f(st['input'])} / 缓存创建 {_f(st['cache_create'])} / 缓存命中 {_f(st['cache_read'])})"
            )
            lines.append(f"  📤 输出 <code>{_f(st['output'])}</code>")
            lines.append(f"  🎯 <b>缓存命中率 {st['cache_hit_rate'] * 100:.1f}%</b>")
        return ("\n" + "\n".join(lines)) if lines else ""

    def parse_event(self, line: str) -> list[tuple[str, str]]:
        """jsonl 一行 → events 列表。
        区分 assistant_tools (thinking + tool_use) 和 assistant_text (真说话)
        给聚合器用 (tools 合并到可编辑消息, text 单独发)。"""
        try:
            j = json.loads(line)
        except Exception:
            return []
        # ★ subagent (Agent tool) 内部对话以 isSidechain=true 写进主 jsonl, 非主会话真输出,
        # 不推 TG — 否则派一次 subagent 会把整个子 agent transcript 回吐 (flood 隐患)
        if j.get("isSidechain"):
            return []
        t = j.get("type")

        if t == "user":
            msg = j.get("message") or {}
            c = msg.get("content")
            if isinstance(c, str):
                return [("user", c)]
            return []

        if t == "assistant":
            msg = j.get("message") or {}
            content = msg.get("content") or []
            text_parts: list[str] = []
            tool_parts: list[str] = []
            for blk in content:
                if not isinstance(blk, dict):
                    continue
                bt = blk.get("type")
                if bt == "text":
                    text_parts.append(html.escape(blk.get("text", "")))
                elif bt == "thinking":
                    tx = (blk.get("thinking") or "").strip()
                    if tx:
                        tool_parts.append(
                            f"💭 <i>{html.escape(tx[:300])}{'…' if len(tx) > 300 else ''}</i>"
                        )
                elif bt == "tool_use":
                    tool_parts.append(format_tool_use(blk.get("name", "?"), blk.get("input") or {}))
            # 注: AskUserQuestion 已全局封禁, picker 兜底由 detect_idle_picker 处理
            events: list[tuple[str, str]] = []
            if tool_parts:
                events.append(("assistant_tools", "\n".join(tool_parts)))
            if text_parts:
                events.append(("assistant_text", "\n".join(text_parts)))
            return events

        if t == "attachment":
            # 所有 attachment 事件都不推到 TG:
            #   - Boss 在 TG 发图片是主动行为,bot 回声 "📎 附件" 无意义
            #   - claude 自动注入的 compact_file_reference / file / date_change /
            #     deferred_tools_delta / hook_* 等都是内部事件,Boss 不需要看
            return []

        return []

    def find_tui_activity_fp(self, pane: str) -> str | None:
        status = self.parse_terminal_status(pane)
        if status and status.state == TerminalState.WORKING:
            return status.label
        return None

    async def ensure_running(self, b: "Binding") -> None:
        if not tmux_has_session(b.tmux_session):
            tmux_new_session(b.tmux_session, b.cwd)
            await asyncio.sleep(0.5)
        cmd = tmux_pane_command(b.tmux_target)
        if self.is_running_command(cmd):
            return
        if not self.can_start_from_command(cmd):
            log.warning(
                "[%s] refusing to start claude in pane with foreground command %r",
                b.name,
                cmd,
            )
            return
        start = _start_cmd()
        session_id = b.provider_session_id or b.last_session_id
        if session_id:
            start += f" --resume {session_id}"
        await tmux_send_text(b.tmux_target, start)
        await asyncio.sleep(2.0)

    def command_opts(self) -> dict[str, CmdOpts]:
        return {
            # lines=250: /context 等输出很长(context 满时网格+全分类+MCP+记忆+技能+建议
            # 可达 80+ 行), 默认 80 行抓取会把顶部 token 总量行截掉 → parser 抓不到 → 回退原始屏。
            "/context": CmdOpts(parser=parse_context, lines=250),
            "/cost":    CmdOpts(parser=parse_cost, lines=250),
            "/usage":   CmdOpts(parser=parse_cost, lines=250),
            "/stats":   CmdOpts(parser=parse_cost, lines=250),
            "/status":  CmdOpts(parser=parse_status, lines=250),
            "/help":    CmdOpts(parser=parse_help, lines=250),
            "/compact": CmdOpts(
                # ★ /compact 不切 session_id, 在同一个 jsonl 末尾 append 一条
                # type=system + subtype=compact_boundary 事件 (含 compactMetadata
                # 直接给 pre/postTokens) — 这是唯一可靠硬信号。屏幕 "Compacted
                # (ctrl+o..)" 字样在 120 行 capture 历史里会假阳, 所以不挂
                # done_pattern; pre/post token 由 metadata 直接拿, 不再依赖
                # read_context_size 倒扫 jsonl。
                # max_iters=360: 圆桌脚本类大工程 dur 可超 200s, 加上 jsonl 事务式
                # flush 滞后 30-120s, 给到 360s 窗口 + 5s retry = 367s 总等待裕度;
                # commands.py 已禁 expect_compact_done 时的 stable 早退 (屏幕静止 ≠
                # jsonl flush, claude TUI 事务式 flush 会有 30-120s 滞后)。
                init_delay=2.0, poll=1.0, max_iters=360, lines=120,
                expect_compact_done=True,
                notice="⏳ 压缩中…(可能 2-5 分钟,完成后会发通知)",
                fallback_summary="✅ <b>上下文已压缩</b>\n📜 完整摘要 TUI 内 <code>ctrl+o</code> 查看",
            ),
            "/clear": CmdOpts(parser=parse_clear, init_delay=0.5, poll=0.3, max_iters=15, expect_new_session=True),
            "/new":   CmdOpts(parser=parse_new,   init_delay=0.5, poll=0.3, max_iters=15, expect_new_session=True),
            "/resume":CmdOpts(parser=parse_resume,init_delay=0.5, poll=0.3, max_iters=6),
            "/rename":CmdOpts(parser=parse_rename,init_delay=0.3, poll=0.3, max_iters=4),
        }

    def command_aliases(self) -> dict[str, str]:
        return {"/new": "/clear"}

    def read_context_size(self, jsonl_path: Path | None) -> int | None:
        """从 jsonl 末尾倒着扫, 找最后一条带 usage 的 message, 返回 context size
        (= input_tokens + cache_read + cache_creation, 即真实占用的上下文窗口大小)。

        用于 /compact 显示压缩前后对比。
        """
        if not jsonl_path or not jsonl_path.is_file():
            return None
        try:
            lines = jsonl_path.read_text(errors="replace").splitlines()
        except Exception as e:
            log.debug(f"read_context_size: read err: {e}")
            return None
        for line in reversed(lines):
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            msg = obj.get("message") or {}
            u = msg.get("usage") or {}
            if not u:
                continue
            total = (
                (u.get("input_tokens") or 0)
                + (u.get("cache_read_input_tokens") or 0)
                + (u.get("cache_creation_input_tokens") or 0)
            )
            if total > 0:
                return total
        return None

    def compact_metadata_since(self, jsonl_path: Path | None, since_byte: int = 0) -> dict | None:
        """/compact 完成硬信号 + metadata: 从 since_byte 起新增 jsonl 内容里找
        ``type=system, subtype=compact_boundary`` 事件, 解析 ``compactMetadata``。

        实测字段 (claude 2.1.150)::

            {
              "type": "system",
              "subtype": "compact_boundary",
              "compactMetadata": {
                "trigger": "manual",     # 或 "auto"
                "preTokens": 410228,     # 压缩前 ctx
                "postTokens": 4888,      # 压缩后 ctx
                "durationMs": 127331
              }
            }

        since_byte 限定只看新增部分, 避免历史 marker 假阳。
        """
        if not jsonl_path or not jsonl_path.is_file():
            return None
        try:
            with jsonl_path.open("rb") as f:
                f.seek(since_byte)
                tail = f.read()
        except Exception as e:
            log.debug(f"compact_metadata_since read err: {e}")
            return None
        if not tail or b"compact_boundary" not in tail:
            return None
        for raw in tail.splitlines():
            if not raw or b"compact_boundary" not in raw:
                continue
            try:
                obj = json.loads(raw)
            except Exception:
                continue
            if obj.get("type") == "system" and obj.get("subtype") == "compact_boundary":
                meta = obj.get("compactMetadata") or {}
                return {
                    "preTokens": meta.get("preTokens"),
                    "postTokens": meta.get("postTokens"),
                    "durationMs": meta.get("durationMs"),
                    "trigger": meta.get("trigger"),
                }
        return None
        return False

    def aggregate_usage(self, jsonl_path: Path, last_n: int = 200) -> dict | None:
        try:
            all_lines = jsonl_path.read_text(errors="replace").splitlines()
        except Exception as e:
            log.debug(f"read jsonl err: {e}")
            return None
        total_in = total_out = c_create = c_read = 0
        count = 0
        last_ts = None
        last_model = None
        for line in all_lines[-last_n * 3:]:
            try:
                j = json.loads(line)
            except Exception:
                continue
            if j.get("type") != "assistant":
                continue
            msg = j.get("message") or {}
            u = msg.get("usage") or {}
            total_in   += int(u.get("input_tokens", 0) or 0)
            total_out  += int(u.get("output_tokens", 0) or 0)
            c_create   += int(u.get("cache_creation_input_tokens", 0) or 0)
            c_read     += int(u.get("cache_read_input_tokens", 0) or 0)
            count += 1
            last_ts = j.get("timestamp") or last_ts
            last_model = msg.get("model") or last_model
        if count == 0:
            return None
        billable_in = total_in + c_create + c_read
        cache_hit = c_read / billable_in if billable_in > 0 else 0
        return {
            "count": count,
            "input": total_in,
            "output": total_out,
            "cache_create": c_create,
            "cache_read": c_read,
            "cache_hit_rate": cache_hit,
            "last_ts": last_ts,
            "model": last_model,
        }
