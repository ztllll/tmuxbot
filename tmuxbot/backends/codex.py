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
from tmuxbot.core.capabilities import ProviderCapabilities
from tmuxbot.core.events import ProviderEvent, ProviderEventKind, TerminalState, TerminalStatus
from tmuxbot.core.sessions import SessionIdentity
from tmuxbot.tmux import (
    tmux_capture, tmux_has_session, tmux_new_session,
    tmux_pane_command, tmux_safe_launch, tmux_send_key,
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
            lines = [zh]
            explanation = str(args.get("explanation") or "").strip()
            if explanation:
                lines.append(f"<i>{html.escape(explanation[:300])}</i>")
            status_label = {
                "completed": "✓",
                "in_progress": "→",
                "pending": "·",
            }
            for item in plan[:12]:
                if not isinstance(item, dict):
                    continue
                step = str(item.get("step") or "").strip()
                status = str(item.get("status") or "").strip()
                if not step:
                    continue
                mark = status_label.get(status, "·")
                status_text = f" <code>{html.escape(status)}</code>" if status else ""
                lines.append(f"{mark} {html.escape(step[:180])}{status_text}")
            if len(plan) > 12:
                lines.append(f"… 还有 {len(plan) - 12} 项")
            if len(lines) > 1:
                return "\n".join(lines)
        return zh
    if name in ("search", "web_search"):
        q = str(args.get("query") or args.get("q", ""))[:120]
        return f"{zh} <code>{html.escape(q)}</code>"
    # fallback: 取第一个 key
    k = next(iter(args))
    v = str(args[k])[:120]
    return f"{zh} <i>{html.escape(k)}={html.escape(v)}</i>"


def _format_codex_custom_tool(name: str, input_text: str) -> str:
    zh = CODEX_TOOL_ZH.get(name, f"🛠 {name}")
    if name == "apply_patch":
        files = _patch_file_names(input_text)
        if files:
            shown = ", ".join(html.escape(f) for f in files[:4])
            suffix = f" +{len(files) - 4}" if len(files) > 4 else ""
            return f"{zh} <code>{shown}{suffix}</code>"
    first = input_text.strip().splitlines()[0] if input_text.strip() else ""
    return f"{zh} <code>{html.escape(first[:160])}</code>" if first else zh


def _format_patch_apply_end(payload: dict) -> str:
    success = bool(payload.get("success"))
    text = "\n".join(
        str(payload.get(k) or "").strip()
        for k in ("stdout", "stderr")
        if payload.get(k)
    )
    files = _patch_result_file_names(text)
    if files:
        shown = ", ".join(html.escape(f) for f in files[:4])
        suffix = f" +{len(files) - 4}" if len(files) > 4 else ""
        target = f" <code>{shown}{suffix}</code>"
    else:
        target = ""
    if success:
        return f"✓ 改文件成功{target}"
    detail = text.strip().splitlines()[0] if text.strip() else "apply_patch failed"
    return f"⚠️ 改文件失败{target}\n<code>{html.escape(detail[:220])}</code>"


def _patch_file_names(patch: str) -> list[str]:
    names: list[str] = []
    for line in patch.splitlines():
        m = re.match(r"\*\*\* (?:Add|Update|Delete) File: (.+)", line.strip())
        if m:
            names.append(m.group(1).strip())
    return _unique(names)


def _patch_result_file_names(text: str) -> list[str]:
    names: list[str] = []
    for line in text.splitlines():
        m = re.match(r"\s*(?:M|A|D)\s+(.+)", line)
        if m:
            names.append(m.group(1).strip())
    return _unique(names)


def _unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            out.append(value)
    return out


# ────────── codex TUI 活跃指纹 ──────────
# 例: "• Working (9s • esc to interrupt)" 或 "• Working (1m 23s • esc to interrupt)"
# Boss 看到时间字段在涨 → 视为活跃
_CODEX_BUSY_RE = re.compile(
    r"•\s*Working\s*\(\s*(\d+m\s+\d+s|\d+s)\s*[•·]\s*esc",
    re.I,
)


def _parse_codex_duration(raw: str) -> int:
    minutes = re.search(r"(\d+)m", raw)
    seconds = re.search(r"(\d+)s", raw)
    return (int(minutes.group(1)) * 60 if minutes else 0) + (
        int(seconds.group(1)) if seconds else 0
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
    # npm wrapper 的 pane_current_command 是 node, standalone 包则是 codex。
    pane_command_name = "node"
    pane_command_names = frozenset({"node", "codex"})
    shell_command_names = frozenset({"bash", "zsh", "sh", "fish"})
    start_cmd = START_CMD

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            name=self.name,
            supports_structured_transcript=True,
            supports_incremental_text=True,
            supports_resume=True,
            supports_plans=True,
            supports_usage=True,
            supports_interactive_pickers=True,
        )

    @property
    def running_command_names(self) -> frozenset[str]:
        return self.pane_command_names

    def is_running_command(self, command: str) -> bool:
        return command in self.pane_command_names

    def parse_terminal_status(self, pane: str) -> TerminalStatus | None:
        clean = strip_decorations(pane)
        if not clean.strip():
            return None

        busy = _CODEX_BUSY_RE.search(clean)
        state = TerminalState.WORKING if busy else TerminalState.IDLE
        duration = _parse_codex_duration(busy.group(1)) if busy else None

        model = effort = cwd = None
        status_match = re.search(
            r"^\s*(gpt-[\w.-]+)(?:\s+([\w-]+))?\s*[·•]\s*(~?/\S+|/\S+)\s*$",
            clean,
            re.M | re.I,
        )
        if status_match:
            model, effort, cwd = status_match.groups()

        permission = None
        if re.search(r"\bYOLO(?: mode)?\b", clean, re.I):
            permission = "YOLO"

        return TerminalStatus(
            state=state,
            label=busy.group(0).strip() if busy else "ready",
            model=model,
            effort=effort,
            permission_mode=permission,
            cwd=cwd,
            duration_seconds=duration,
        )

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
        if b.transcript_path:
            pinned = Path(b.transcript_path)
            metadata = self._session_metadata(pinned)
            if metadata and self._metadata_matches(
                metadata, target_cwd, b.provider_session_id
            ):
                return pinned
        if b.provider_session_id:
            for jl in all_files:
                metadata = self._session_metadata(jl)
                if metadata and self._metadata_matches(
                    metadata, target_cwd, b.provider_session_id
                ):
                    return jl
        # 按 mtime 倒序, 找第一个 cwd 匹配的。找不到就返回 None, 不能兜底到全局最新,
        # 否则多个 binding 会同时 tail 同一个 Codex rollout, 导致跨 chat 推送。
        for jl in sorted(all_files, key=lambda p: p.stat().st_mtime, reverse=True):
            metadata = self._session_metadata(jl)
            if metadata and self._metadata_matches(metadata, target_cwd, None):
                return jl
        return None

    @staticmethod
    def _session_metadata(jl: Path) -> dict | None:
        if not jl.is_file():
            return None
        try:
            with open(jl, "r", encoding="utf-8", errors="replace") as f:
                first = f.readline()
            if not first.strip():
                return None
            row = json.loads(first)
            if row.get("type") != "session_meta":
                return None
            payload = row.get("payload", {}) or {}
            return payload if isinstance(payload, dict) else None
        except Exception:
            return None

    @staticmethod
    def _metadata_matches(
        metadata: dict, target_cwd: str, session_id: str | None
    ) -> bool:
        cwd = metadata.get("cwd")
        if not isinstance(cwd, str) or str(Path(cwd).resolve()) != target_cwd:
            return False
        if session_id is None:
            return True
        actual_id = metadata.get("id") or metadata.get("session_id")
        return actual_id == session_id

    def session_identity(self, b: "Binding", transcript_path: Path) -> SessionIdentity:
        metadata = self._session_metadata(transcript_path) or {}
        session_id = metadata.get("id") or metadata.get("session_id") or transcript_path.stem
        return SessionIdentity(
            provider=self.name,
            session_id=str(session_id),
            transcript_path=str(transcript_path),
            tmux_target=b.tmux_target,
            cwd=str(b.cwd),
        )

    def parse_event(
        self, line: str, provider_session_id: str | None = None
    ) -> list[ProviderEvent]:
        """Codex rollout row → normalized provider events."""
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
            if pt == "patch_apply_end":
                return [
                    self.provider_event(
                        j,
                        ProviderEventKind.TOOL_PROGRESS,
                        _format_patch_apply_end(p),
                        provider_session_id=provider_session_id,
                        native_id=p.get("call_id") or p.get("id"),
                    )
                ]
            if pt == "agent_message":
                message = str(p.get("message") or "").strip()
                if message:
                    return [
                        self.provider_event(
                            j,
                            ProviderEventKind.FINAL_TEXT,
                            html.escape(message),
                            provider_session_id=provider_session_id,
                            native_id=p.get("id"),
                            phase="live",
                        )
                    ]
                return []
            if pt == "agent_message_delta":
                delta = str(
                    p.get("delta")
                    or p.get("message")
                    or p.get("text")
                    or ""
                )
                if delta:
                    return [
                        self.provider_event(
                            j,
                            ProviderEventKind.TEXT_DELTA,
                            html.escape(delta),
                            provider_session_id=provider_session_id,
                            native_id=p.get("id"),
                        )
                    ]
                return []
            if pt in ("task_started", "task_complete"):
                return [
                    self.provider_event(
                        j,
                        ProviderEventKind.LIFECYCLE_CHANGE,
                        str(pt),
                        provider_session_id=provider_session_id,
                        native_id=p.get("id"),
                        metadata={"lifecycle": pt},
                    )
                ]
            # 这些都跟 response_item 重复或纯 metadata, 跳过
            if pt in ("token_count", "user_message",
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
                    return [
                        self.provider_event(
                            j,
                            ProviderEventKind.FINAL_TEXT,
                            "\n".join(parts),
                            provider_session_id=provider_session_id,
                            native_id=p.get("id"),
                        )
                    ]
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
                    return [
                        self.provider_event(
                            j,
                            ProviderEventKind.TOOL_PROGRESS,
                            "💭 <i>" + html.escape(" / ".join(texts)) + "</i>",
                            provider_session_id=provider_session_id,
                            native_id=p.get("id"),
                        )
                    ]
                return []
            if pt == "function_call":
                name = p.get("name", "?")
                args_json = p.get("arguments", "")
                kind = (
                    ProviderEventKind.PLAN_UPDATE
                    if name == "update_plan"
                    else ProviderEventKind.TOOL_PROGRESS
                )
                return [
                    self.provider_event(
                        j,
                        kind,
                        _format_codex_tool(name, args_json),
                        provider_session_id=provider_session_id,
                        native_id=p.get("call_id") or p.get("id"),
                    )
                ]
            if pt == "custom_tool_call":
                name = p.get("name", "?")
                input_text = str(p.get("input") or "")
                return [
                    self.provider_event(
                        j,
                        ProviderEventKind.TOOL_PROGRESS,
                        _format_codex_custom_tool(name, input_text),
                        provider_session_id=provider_session_id,
                        native_id=p.get("call_id") or p.get("id"),
                    )
                ]
            if pt == "custom_tool_call_output":
                output = str(p.get("output") or "")
                if re.search(r"failed|error|traceback", output, re.I):
                    detail = output.strip().splitlines()[0] if output.strip() else "tool failed"
                    return [
                        self.provider_event(
                            j,
                            ProviderEventKind.PROVIDER_ERROR,
                            f"⚠️ 工具失败 <code>{html.escape(detail[:220])}</code>",
                            provider_session_id=provider_session_id,
                            native_id=p.get("call_id") or p.get("id"),
                        )
                    ]
                return []
            if pt == "function_call_output":
                return []
            return []

        return []

    def find_tui_activity_fp(self, pane: str) -> str | None:
        """codex active 指示: '• Working (Xs • esc to interrupt)' (无 token 字段)"""
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
                "[%s] refusing to start codex in pane with foreground command %r",
                b.name,
                cmd,
            )
            return
        # codex 没有 --resume <session_id> 直传方式 (只能通过 /resume 命令)
        launched = await tmux_safe_launch(
            b.tmux_target,
            self.start_cmd,
            allowed_shells=self.shell_command_names,
        )
        if not launched:
            log.warning("[%s] codex launch aborted after foreground revalidation", b.name)
            return
        # codex 冷启动慢 + 可能弹 trust/update picker。轮询处理直到真就绪:
        #  - update picker: 绝不选 "Update now"(会 npm install 后退出回 bash 无法自愈),
        #    发 Esc 取消/跳过
        #  - trust picker: 新 /init 目录首次进会弹, 选信任 (Yes 默认高亮第一项, Enter)
        #  - 真就绪: pane=node/codex 且出现输入符 '›' / 状态行 'gpt-' 且无 picker
        #  否则盲发消息会落进 picker 丢失
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
            if (
                    self.is_running_command(tmux_pane_command(b.tmux_target))
                and ("›" in scr or "gpt-" in low)
            ):
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
        total_in = total_out = cached = 0
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
