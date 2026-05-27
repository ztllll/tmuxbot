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
import re
import time
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from tmuxbot.backends.base import Backend, CmdOpts
from tmuxbot.quota import fetch_quota
from tmuxbot.tmux import tmux_has_session, tmux_new_session, tmux_pane_command, tmux_send_text
from tmuxbot.utils import encode_cwd, render_table, strip_decorations

if TYPE_CHECKING:
    from tmuxbot.state import Binding

log = logging.getLogger("tmuxbot")

CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"
START_CMD = "claude --dangerously-skip-permissions"


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
    total_m = re.search(
        r"(\d+(?:\.\d+)?[kmKM])\s*/\s*(\d+[kmKM])\s*tokens?\s*\((\d+)%\)",
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

    if limit_rows:
        parts.append("📈 <b>限制窗口</b>")
        parts.append(f"<pre>{html.escape(render_table(['窗口', '已用', '进度', '重置时间'], limit_rows))}</pre>")

    # 旧版 /cost 兜底
    if not limit_rows:
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
    """把 /api/oauth/usage 的 payload 渲染成 /status 的配额章节 (HTML)"""
    lines = ["", "🚦 <b>订阅配额</b>"]
    if not payload:
        reason = f" ({last_error})" if last_error else ""
        lines.append(f"  · 无法读取{html.escape(reason)}")
        return lines

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


# ────────── ClaudeCodeBackend ──────────
class ClaudeCodeBackend(Backend):
    name = "claude_code"
    pane_command_name = "claude"
    start_cmd = START_CMD

    # 给 BotFather 注册菜单 (M3 codex 可以有不同清单)
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
        files = list(d.glob("*.jsonl"))
        if not files:
            return None
        return max(files, key=lambda p: p.stat().st_mtime)

    def parse_event(self, line: str) -> list[tuple[str, str]]:
        """jsonl 一行 → events 列表。
        区分 assistant_tools (thinking + tool_use) 和 assistant_text (真说话)
        给聚合器用 (tools 合并到可编辑消息, text 单独发)。"""
        try:
            j = json.loads(line)
        except Exception:
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
            att = j.get("attachment") or {}
            att_t = att.get("type", "")
            if att_t.startswith("hook_") or att.get("hookEvent"):
                return []
            if att_t in ("image", "image_url"):
                return [("attachment", "📷 收到图片附件")]
            if att_t in ("file", "document"):
                name = att.get("name") or att.get("path") or ""
                return [("attachment", f"📎 附件 <code>{html.escape(str(name)[:80])}</code>")]
            return []

        return []

    def find_tui_activity_fp(self, pane: str) -> str | None:
        clean = strip_decorations(pane)
        for line in clean.splitlines():
            if _TUI_TIME_RE.search(line) and _TUI_TOK_RE.search(line):
                return line.strip()
        return None

    async def ensure_running(self, b: "Binding") -> None:
        if not tmux_has_session(b.tmux_session):
            tmux_new_session(b.tmux_session, b.cwd)
            await asyncio.sleep(0.5)
        cmd = tmux_pane_command(b.tmux_target)
        if cmd != self.pane_command_name:
            start = self.start_cmd
            if b.last_session_id:
                start += f" --resume {b.last_session_id}"
            await tmux_send_text(b.tmux_target, start)
            await asyncio.sleep(2.0)

    def command_opts(self) -> dict[str, CmdOpts]:
        return {
            "/context": CmdOpts(parser=parse_context),
            "/cost":    CmdOpts(parser=parse_cost),
            "/usage":   CmdOpts(parser=parse_cost),
            "/stats":   CmdOpts(parser=parse_cost),
            "/status":  CmdOpts(parser=parse_status),
            "/help":    CmdOpts(parser=parse_help, lines=200),
            "/compact": CmdOpts(
                parser=parse_compact,
                init_delay=2.0, poll=1.0, max_iters=120, lines=120,
                parser_can_retry=True,
                done_pattern=COMPACT_DONE_RE,
                expect_new_session=True,
                notice="⏳ 压缩中…(可能 10-60s,完成后会发完成通知)",
                fallback_summary="✅ <b>上下文已压缩</b>\n📜 完整摘要 TUI 内 <code>ctrl+o</code> 查看",
            ),
            "/clear": CmdOpts(parser=parse_clear, init_delay=0.5, poll=0.3, max_iters=15, expect_new_session=True),
            "/new":   CmdOpts(parser=parse_new,   init_delay=0.5, poll=0.3, max_iters=15, expect_new_session=True),
            "/resume":CmdOpts(parser=parse_resume,init_delay=0.5, poll=0.3, max_iters=6),
            "/rename":CmdOpts(parser=parse_rename,init_delay=0.3, poll=0.3, max_iters=4),
        }

    def command_aliases(self) -> dict[str, str]:
        return {"/new": "/clear"}

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
