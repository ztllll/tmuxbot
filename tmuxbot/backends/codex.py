"""Codex CLI backend (OpenAI `@openai/codex`, Rust binary)。

接入 P4 工作。差异点 vs ClaudeCodeBackend:
- jsonl 路径: ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl (按日期分层, 不是 encoded-cwd)
- jsonl schema: RolloutLine + RolloutItem (response_item / event_msg / session_meta / turn_context)
- 工具名: codex 自己一套 (exec_command / update_plan / apply_patch)
- TUI active 指示: `• Working (Xs • esc to interrupt)` — 无 token 计数, 只有时间
- 启动命令: `codex` (二进制在 PATH 上)

数据探针结论 (基于本机 ~/.codex/sessions/ 真实 jsonl 样本):
- 一条 response_item 含 payload.type 区分 message / reasoning / function_call / function_call_output
- assistant text 在 message role=assistant content[i].type=output_text
- thinking 在 reasoning (通常 summary[] / content 空, encrypted_content 不解析)
- tool_use 在 function_call (name + arguments JSON string)
- agent_message event_msg 跟 response_item message 内容重复 → 跳过避免双推
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING

from tmuxbot.backends.base import Backend, CmdOpts
from tmuxbot.tmux import (
    tmux_capture, tmux_has_session, tmux_new_session,
    tmux_pane_command, tmux_send_key, tmux_send_text,
)
from tmuxbot.utils import strip_decorations

if TYPE_CHECKING:
    from tmuxbot.state import Binding

log = logging.getLogger("tmuxbot")

CODEX_SESSIONS_DIR = Path.home() / ".codex" / "sessions"
# --dangerously-bypass-approvals-and-sandbox: codex 最高权限(跳过所有审批 + 无沙箱),
# 等价 claude 的 --dangerously-skip-permissions。bot 是 tmux 桥接, 命令需无人值守自动执行。
# CODEX_BIN 仍可配绝对路径(防 tmux shell PATH 不含 ~/.npm-global/bin)。
START_CMD = f'{os.getenv("CODEX_BIN", "codex")} --dangerously-bypass-approvals-and-sandbox'


# ────────── 工具名中文化 (codex 工具集) ──────────
CODEX_TOOL_ZH = {
    "exec_command":         "💻 执行",
    "update_plan":          "📋 更新计划",
    "apply_patch":          "✂️ 改文件",
    "read_file":            "📖 读取",
    "write_file":           "✏️ 写入",
    "search":               "🔍 搜索",
    "view_image":           "🖼 看图",
    "open_link":            "🌐 打开链接",
    "web_search":           "🌐 联网搜索",
    "fetch":                "🌐 抓取",
}


def _format_codex_tool(name: str, args_json: str) -> str:
    """渲染 codex function_call (name + arguments JSON) 成 TG 卡片"""
    zh = CODEX_TOOL_ZH.get(name, f"🛠 {name}")
    try:
        args = json.loads(args_json) if isinstance(args_json, str) else (args_json or {})
    except Exception:
        return zh
    if not isinstance(args, dict) or not args:
        return zh
    # 命中关键字段渲染
    if name == "exec_command":
        cmd = str(args.get("cmd") or args.get("command", ""))[:150]
        return f"{zh} <code>{html.escape(cmd)}</code>"
    if name in ("read_file", "write_file"):
        p = str(args.get("path") or args.get("file_path", ""))[:120]
        return f"{zh} <code>{html.escape(p)}</code>"
    if name == "apply_patch":
        # apply_patch 的 args 通常含 input (patch 文本), 截取首行
        inp = str(args.get("input", ""))[:120].splitlines()[0] if args.get("input") else ""
        return f"{zh} <code>{html.escape(inp)}</code>"
    if name == "update_plan":
        plan = args.get("plan") or []
        if isinstance(plan, list) and plan:
            top = next((p.get("step", "") for p in plan if p.get("status") == "in_progress"), "")
            if top:
                return f"{zh} <i>{html.escape(str(top)[:80])}</i>"
        return zh
    if name in ("search", "web_search"):
        q = str(args.get("query") or args.get("q", ""))[:120]
        return f"{zh} <code>{html.escape(q)}</code>"
    # fallback: 取第一个 key
    k = next(iter(args))
    v = str(args[k])[:120]
    return f"{zh} <i>{html.escape(k)}={html.escape(v)}</i>"


# ────────── codex TUI 活跃指纹 ──────────
# 例: "• Working (9s • esc to interrupt)" 或 "• Working (1m 23s • esc to interrupt)"
# Boss 看到时间字段在涨 → 视为活跃
_CODEX_BUSY_RE = re.compile(
    r"•\s*Working\s*\(\s*(\d+m\s+\d+s|\d+s)\s*[•·]\s*esc",
    re.I,
)


# ────────── 命令 parser (codex 命令输出格式可能跟 claude 不同, 用最小集) ──────────
def parse_status_codex(raw: str) -> str | None:
    """/status → codex 的状态格式: 'model:' / 'directory:' / 'session:' 等"""
    clean = strip_decorations(raw)
    kvs = re.findall(r"^\s*(\w[\w\s]*):\s*(\S.*?)\s*$", clean, re.M)
    if not kvs:
        return None
    parts = ["ℹ️ <b>Codex 状态</b>"]
    seen = set()
    for k, v in kvs[:15]:
        k = k.strip()
        if k.lower() in seen:
            continue
        seen.add(k.lower())
        parts.append(f"  · <b>{html.escape(k)}</b>: {html.escape(v.strip()[:200])}")
    return "\n".join(parts) if len(parts) > 1 else None


def parse_clear_codex(raw: str) -> str | None:
    return "🧹 <b>Codex 会话已清空</b>\n· 新 session 已开启"


def parse_new_codex(raw: str) -> str | None:
    return "🆕 <b>Codex 新会话已开启</b>"


# codex compact 完成关键字 (跟 claude 通用)
CODEX_COMPACT_DONE_RE = re.compile(
    r"Compacted|compact.*complete|压缩完成|context\s+compacted",
    re.I,
)


def parse_compact_codex(raw: str) -> str | None:
    clean = strip_decorations(raw)
    if not CODEX_COMPACT_DONE_RE.search(clean):
        return None
    return "✅ <b>Codex 上下文已压缩</b>"


# ────────── CodexBackend ──────────
class CodexBackend(Backend):
    name = "codex"
    # ★ codex v0.124.0 是 node CLI, tmux pane_current_command 显示 "node" 不是 "codex"
    # (pane 专用: 非 bash 即 codex-as-node, 用 "node" 判活)
    pane_command_name = "node"
    start_cmd = START_CMD

    bot_commands = [
        ("status", "ℹ️ Codex 状态"),
        ("info", "📊 累计 token (jsonl)"),
        ("whoami", "👤 我的 user_id / chat_id"),
        ("new", "🆕 开新会话"),
        ("resume", "🔄 恢复历史会话"),
        ("esc", "⎋ 中断当前生成"),
        ("cc", "⌃C 取消/清空输入"),
        ("eof", "⌃D 退出 codex"),
        ("screen", "📷 抓 tmux 屏幕"),
        ("restart", "🔄 重启 codex"),
    ]

    def find_active_jsonl(self, b: "Binding") -> Path | None:
        """扫 ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl, mtime 最新且 cwd 匹配的。
        codex 没把 cwd 编码进路径, 需要读 session_meta.payload.cwd 跟 binding.cwd 对比。"""
        if not CODEX_SESSIONS_DIR.exists():
            return None
        all_files = list(CODEX_SESSIONS_DIR.rglob("rollout-*.jsonl"))
        if not all_files:
            return None
        target_cwd = str(b.cwd.resolve())
        # 按 mtime 倒序, 找第一个 cwd 匹配的
        for jl in sorted(all_files, key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                with open(jl, "r", encoding="utf-8", errors="replace") as f:
                    first = f.readline()
                if not first.strip():
                    continue
                j = json.loads(first)
                if j.get("type") == "session_meta":
                    p = j.get("payload", {}) or {}
                    if p.get("cwd") == target_cwd:
                        return jl
            except Exception:
                continue
        # 兜底: 返回 mtime 最新, 不强求 cwd 匹配
        return max(all_files, key=lambda p: p.stat().st_mtime)

    def parse_event(self, line: str) -> list[tuple[str, str]]:
        """codex jsonl 一行 → events 列表。
        - session_meta / turn_context / task_started / token_count: 跳过
        - event_msg.user_message: 跳过 (跟 response_item user 重复)
        - event_msg.agent_message: 跳过 (跟 response_item assistant message 重复)
        - response_item message role=user: ("user", ...) 不回声
        - response_item message role=assistant: ("assistant_text", text)
        - response_item reasoning: ("assistant_tools", 💭 thinking) 通常 empty 跳过
        - response_item function_call: ("assistant_tools", tool_use 卡片)
        - response_item function_call_output: 跳过 (类似 claude 的 tool_result)
        """
        try:
            j = json.loads(line)
        except Exception:
            return []
        t = j.get("type")
        p = j.get("payload") or {}

        if t == "session_meta":
            return []
        if t == "turn_context":
            return []
        if t == "event_msg":
            pt = p.get("type")
            # 这些都跟 response_item 重复或纯 metadata, 跳过
            if pt in ("task_started", "task_complete", "token_count",
                      "user_message", "agent_message", "agent_message_delta",
                      "agent_reasoning_delta", "agent_reasoning"):
                return []
            return []

        if t == "response_item":
            pt = p.get("type")
            if pt == "message":
                role = p.get("role", "")
                content = p.get("content") or []
                if role == "user" or role == "developer":
                    # 用户/系统输入 — 不回声给 Boss
                    return []
                if role == "assistant":
                    parts = []
                    for c in content:
                        if not isinstance(c, dict):
                            continue
                        ct = c.get("type")
                        if ct == "output_text":
                            parts.append(html.escape(c.get("text", "")))
                    if not parts:
                        return []
                    return [("assistant_text", "\n".join(parts))]
                return []
            if pt == "reasoning":
                # codex reasoning 通常 summary[] / content 都空 (encrypted_content 不解析)
                # 显示一个占位 "💭 思考中…" 让 Boss 知道 codex 在 think
                summary = p.get("summary") or []
                if not summary:
                    return []
                # summary 是 list of dict, 抽 text 字段
                texts = []
                for s in summary:
                    if isinstance(s, dict):
                        tx = s.get("text") or s.get("content") or ""
                        if tx:
                            texts.append(str(tx)[:300])
                if texts:
                    return [("assistant_tools", "💭 <i>" + html.escape(" / ".join(texts)) + "</i>")]
                return []
            if pt == "function_call":
                name = p.get("name", "?")
                args_json = p.get("arguments", "")
                return [("assistant_tools", _format_codex_tool(name, args_json))]
            if pt == "function_call_output":
                return []
            return []

        return []

    def find_tui_activity_fp(self, pane: str) -> str | None:
        """codex active 指示: '• Working (Xs • esc to interrupt)' (无 token 字段)"""
        clean = strip_decorations(pane)
        m = _CODEX_BUSY_RE.search(clean)
        if m:
            return m.group(0).strip()
        return None

    async def ensure_running(self, b: "Binding") -> None:
        if not tmux_has_session(b.tmux_session):
            tmux_new_session(b.tmux_session, b.cwd)
            await asyncio.sleep(0.5)
        cmd = tmux_pane_command(b.tmux_target)
        if cmd != self.pane_command_name:
            # codex 没有 --resume <session_id> 直传方式 (只能通过 /resume 命令)
            await tmux_send_text(b.tmux_target, self.start_cmd)
            # codex 冷启动慢 + 可能弹 trust/update picker。轮询处理直到真就绪:
            #  - update picker: 绝不选 "Update now"(会 npm install 后退出回 bash 无法自愈),
            #    发 Esc 取消/跳过
            #  - trust picker: 新 /init 目录首次进会弹, 选信任 (Yes 默认高亮第一项, Enter)
            #  - 真就绪: pane=node 且出现输入符 '›' / 状态行 'gpt-' 且无 picker
            #  否则盲发消息会落进 picker 丢失 (旧实现只看 pane=node 会被 picker 骗)
            for _ in range(40):  # 最多 ~20s
                await asyncio.sleep(0.5)
                try:
                    scr = tmux_capture(b.tmux_target, 25)
                except Exception:
                    continue
                low = scr.lower()
                if "update" in low and ("update now" in low or "skip" in low):
                    tmux_send_key(b.tmux_target, "Escape")   # 取消更新, 绝不 Update now
                    continue
                if ("trust" in low or "信任" in low) and ("yes" in low or "no" in low or "1." in scr):
                    tmux_send_key(b.tmux_target, "Enter")    # 选信任 (默认 Yes)
                    continue
                if tmux_pane_command(b.tmux_target) == self.pane_command_name and ("›" in scr or "gpt-" in low):
                    break
            await asyncio.sleep(1.0)  # prompt 渲染余量

    def command_opts(self) -> dict[str, CmdOpts]:
        return {
            # codex 实际跑这些命令时屏幕反馈格式跟 claude 不同, 但 fallback raw 也能用
            "/status":  CmdOpts(parser=parse_status_codex, lines=250),  # 长输出抓全(同 claude)
            "/clear":   CmdOpts(
                parser=parse_clear_codex,
                init_delay=0.5, poll=0.3, max_iters=15, expect_new_session=True,
            ),
            "/new":     CmdOpts(
                parser=parse_new_codex,
                init_delay=0.5, poll=0.3, max_iters=15, expect_new_session=True,
            ),
            "/compact": CmdOpts(
                parser=parse_compact_codex,
                init_delay=2.0, poll=1.0, max_iters=120, lines=120,
                parser_can_retry=True,
                done_pattern=CODEX_COMPACT_DONE_RE,
                expect_new_session=True,
                notice="⏳ Codex 压缩中…",
                fallback_summary="✅ <b>Codex 上下文已压缩</b>",
            ),
            "/resume":  CmdOpts(init_delay=0.5, poll=0.3, max_iters=6),
        }

    def command_aliases(self) -> dict[str, str]:
        """codex 原生有 /new, 不需要别名映射"""
        return {}

    def aggregate_usage(self, jsonl_path: Path, last_n: int = 200) -> dict | None:
        """codex token usage 在 event_msg.token_count.payload.info.total_token_usage"""
        try:
            all_lines = jsonl_path.read_text(errors="replace").splitlines()
        except Exception as e:
            log.debug(f"read codex jsonl err: {e}")
            return None
        total_in = total_out = cached = reasoning_out = 0
        count = 0
        last_ts = None
        model = None
        for line in all_lines[-last_n * 5:]:  # codex jsonl 行更多, 多扫些
            try:
                j = json.loads(line)
            except Exception:
                continue
            t = j.get("type")
            if t == "session_meta":
                p = j.get("payload") or {}
                model = p.get("model_provider") or model
                continue
            if t == "event_msg":
                p = j.get("payload") or {}
                if p.get("type") != "token_count":
                    continue
                info = p.get("info") or {}
                usage = info.get("total_token_usage") or {}
                if usage:
                    # codex 是累计值, 用最后一次即可
                    total_in = int(usage.get("input_tokens", 0) or 0)
                    total_out = int(usage.get("output_tokens", 0) or 0)
                    cached = int(usage.get("cached_input_tokens", 0) or 0)
                    reasoning_out = int(usage.get("reasoning_output_tokens", 0) or 0)
                    last_ts = j.get("timestamp") or last_ts
                continue
            if t == "response_item" and (j.get("payload") or {}).get("type") == "message":
                if (j.get("payload") or {}).get("role") == "assistant":
                    count += 1
        if total_in == 0 and total_out == 0:
            return None
        cache_hit = cached / total_in if total_in > 0 else 0
        return {
            "count": count,
            "input": total_in - cached,
            "output": total_out,
            "cache_create": 0,            # codex 没单独 cache_create 字段
            "cache_read": cached,
            "cache_hit_rate": cache_hit,
            "last_ts": last_ts,
            "model": model or "codex",
        }
