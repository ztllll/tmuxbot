"""Telegram 前端: aiogram 装配 + 命令注册 + 发送/编辑/反应/typing。

每个 frontend = 一个 bot token + 一个 backend + 一组 bindings 子集。
多 bot 共存: __main__ 装配多个 TelegramFrontend 实例并发 polling。
"""
from __future__ import annotations

import asyncio
import html
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Callable, TYPE_CHECKING

import yaml
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramNetworkError,
    TelegramRetryAfter,
)
from aiogram.filters import Command
from aiogram.types import (
    BotCommand,
    BufferedInputFile,
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    ReactionTypeEmoji,
)

from tmuxbot.command_adapter import (
    binding_by_token,
    binding_token,
    handle_semantic_action,
    handle_tui_action,
    semantic_actions_from_body,
)
from tmuxbot.attachments import (
    attachment_ref,
    attachment_path,
    attachment_prompt,
    is_image_file,
    split_outbound_attachments,
)
from tmuxbot.addressing import incoming_message_is_addressed
from tmuxbot.channels.telegram import (
    TelegramChannelAdapter,
    telegram_mentions_bot,
    telegram_replies_to_bot,
)
from tmuxbot.core.capabilities import ChannelCapabilities
from tmuxbot.core.replies import ReplyEnvelope
from tmuxbot.frontends.base import Frontend
from tmuxbot.lifecycle import ensure_binding_running
from tmuxbot.replies import render_assistant_reply, screen_footer_from_capture
from tmuxbot.tmux import tmux_capture, tmux_send_key
from tmuxbot.utils import render_table, utf16_len

if TYPE_CHECKING:
    from tmuxbot.backends.base import Backend
    from tmuxbot.state import Binding, State

log = logging.getLogger("tmuxbot")

# ────────── 常量 ──────────
TG_SPLIT = 3800
TG_DOC_THRESHOLD = 8000
TG_REPLY_FULL_OUTPUT_THRESHOLD = 5000
MAX_FILE_MB = 19
ACK_REACTION = "👀"
UNKNOWN_CHAT_INIT_GRACE_SECONDS = 60.0


def should_grace_unknown_chat(
    *,
    setup_mode: bool,
    bound_count: int,
    removed: bool,
) -> bool:
    """Whether Telegram should keep an unbound chat briefly for /init.

    Bound chats and removal events are handled elsewhere. Unknown chats get a
    short grace window; normal message ACL still only lets Boss run /init.
    """
    if setup_mode or removed or bound_count > 0:
        return False
    return True


# ────────── 工具函数 (TG-specific) ──────────
def split_for_tg(text: str, limit: int = TG_SPLIT) -> list[str]:
    if utf16_len(text) <= limit:
        return [text]
    chunks, cur, cur_len = [], [], 0
    for line in text.split("\n"):
        ll = utf16_len(line) + 1
        if cur_len + ll > limit and cur:
            chunks.append("\n".join(cur))
            cur, cur_len = [line], ll
        else:
            cur.append(line)
            cur_len += ll
    if cur:
        chunks.append("\n".join(cur))
    return chunks


def source_key(m: Message) -> tuple[int, int | None]:
    if m.chat.type == "private":
        return (m.chat.id, None)
    if getattr(m, "is_topic_message", False):
        return (m.chat.id, m.message_thread_id)
    return (m.chat.id, None)


def thread_id_of(m: Message) -> int | None:
    """跟 source_key 一致的 thread_id 提取"""
    if m.chat.type == "private":
        return None
    if getattr(m, "is_topic_message", False):
        return m.message_thread_id
    return None


def telegram_message_mentions_bot(m: Message, bot_username: str | None) -> bool:
    return telegram_mentions_bot(m, bot_username)


def telegram_message_replies_to_bot(
    m: Message, bot_username: str | None, bot_id: int | None
) -> bool:
    return telegram_replies_to_bot(m, bot_username, bot_id)


def telegram_message_addresses_bot(
    m: Message, bot_username: str | None, bot_id: int | None
) -> bool:
    return telegram_message_mentions_bot(m, bot_username) or telegram_message_replies_to_bot(
        m, bot_username, bot_id
    )


def _message_attachment(m: Message) -> tuple[str, str, int | None, str] | None:
    """Return (file_id, filename, file_size, kind) for Telegram file-like messages."""
    if m.photo:
        photo = m.photo[-1]
        return photo.file_id, f"photo_{m.message_id}.jpg", photo.file_size, "photo"

    for attr, fallback in (
        ("document", "document.bin"),
        ("video", "video.mp4"),
        ("animation", "animation.gif"),
        ("audio", "audio.mp3"),
        ("voice", "voice.ogg"),
    ):
        obj = getattr(m, attr, None)
        if obj is None:
            continue
        filename = getattr(obj, "file_name", None) or fallback
        return obj.file_id, filename, getattr(obj, "file_size", None), attr

    return None


def safe_filename_fragment(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip(".-")
    return cleaned or "reply"


# ────────── TelegramFrontend ──────────
class TelegramFrontend(Frontend):
    """Telegram bot 前端。装配 aiogram Bot + Dispatcher, 注册命令/handlers。"""

    name = "telegram"
    capabilities = ChannelCapabilities(
        name="telegram",
        supports_edit=True,
        supports_actions=True,
        supports_threads=True,
        supports_cards=True,
        supports_images=True,
        supports_files=True,
        supports_typing=True,
        supports_replies=True,
        max_text_length=4096,
    )

    def __init__(
        self,
        token: str,
        state: "State",
        backend: "Backend",                 # ★ 单 backend (1 bot ↔ 1 backend)
        bindings: list["Binding"],          # ★ 只接这些 binding (其他 bot 各管自己的)
        env_file: Path,
        bindings_file: Path,
        offsets_file: Path | None = None,   # /init 起 tailer 用
        project_base: str = os.path.expanduser("~/projects"),  # /init 新项目目录的父目录
        bot_token_env: str = "TG_BOT_TOKEN",  # 本 frontend 的 token env key (/init 持久化用)
        group_only_when_mentioned: bool = False,
    ) -> None:
        self.token = token
        self.state = state
        self.backend = backend
        self.bindings = bindings
        self.env_file = env_file
        self.bindings_file = bindings_file
        self.offsets_file = offsets_file
        self.project_base = project_base
        self.bot_token_env = bot_token_env
        self.group_only_when_mentioned = group_only_when_mentioned
        self.bot = Bot(token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
        self.dp = Dispatcher()
        self._bot_username: str | None = None  # 懒加载, start_polling 时填入
        self._bot_id: int | None = None
        self.channel_adapter = TelegramChannelAdapter()
        # forum topic 名缓存: key=(chat_id, thread_id) → topic name。
        # 话题名只在 forum_topic_created / _edited 服务消息里, 普通 message 拿不到 →
        # 服务消息进来时缓存, /init 时按 (chat_id, thread_id) 命中, 取代群名。
        self._topic_names: dict[tuple[int, int], str] = {}
        self._unknown_chat_leave_tasks: dict[int, asyncio.Task] = {}
        self._register_handlers()

    def find_binding(self, chat_id: int, thread_id: int | None) -> "Binding | None":
        """只在自己接的 bindings 子集里找, 避免跨 frontend 冲突"""
        for b in self.bindings:
            if b.chat_id == chat_id and b.thread_id == thread_id:
                return b
        return None

    def normalize_incoming(self, message: Message, *, attachments=()):
        adapter = TelegramChannelAdapter(
            bot_username=self._bot_username,
            bot_id=self._bot_id,
        )
        self.channel_adapter = adapter
        return adapter.normalize_incoming(message, attachments=tuple(attachments))

    def _cancel_unknown_chat_leave(self, chat_id: int) -> None:
        task = self._unknown_chat_leave_tasks.pop(chat_id, None)
        if task is not None:
            task.cancel()

    def _list_projects(self) -> str:
        """列 project_base 下的直接子目录, 返回 HTML 文本 (供 /projects 用)"""
        base = self.project_base
        try:
            dirs = sorted(
                d for d in os.listdir(base)
                if os.path.isdir(os.path.join(base, d))
            )
        except OSError:
            dirs = []
        body = "\n".join(f"• {html.escape(d)}" for d in dirs) if dirs else "（空）"
        return (
            f"📂 <b>项目目录</b> (base: <code>{html.escape(base)}</code>)\n"
            f"{body}\n\n"
            "用法: <code>/init &lt;目录名&gt;</code> 绑定; <code>/init</code> 自动用群名新建"
        )

    async def send_status_summary(
        self, b: "Binding", chat_id: int | str, thread_id: int | None
    ) -> None:
        from tmuxbot.commands import inject_slash_and_capture
        from tmuxbot.tmux import tmux_has_session, tmux_pane_command

        backend = self.backend
        alive = tmux_has_session(b.tmux_session)
        pane_cmd = tmux_pane_command(b.tmux_target) if alive else "-"
        screen_footer = screen_footer_from_capture(tmux_capture(b.tmux_target, 12)) or "-"
        conn_rows = [
            ["tmux", "✅ 正常" if alive else "❌ 断开", b.tmux_target],
            ["cwd", "✅", str(b.cwd)],
            ["session", "✅ 活跃" if b.last_session_id else "—", b.last_session_id or "-"],
            ["backend", "🔌", b.backend],
            ["pane cmd", "·", pane_cmd],
            ["屏幕底部", "·", screen_footer],
        ]
        conn_table = render_table(["项目", "状态", "详情"], conn_rows)

        ctx_summary: str | None = None
        try:
            ctx_raw = await inject_slash_and_capture(b, "/context")
            from tmuxbot.backends.claude_code import parse_context as _pc
            ctx_summary = _pc(ctx_raw) if b.backend == "claude_code" else None
        except Exception:
            log.exception("inject /context err")

        usage_summary: str | None = None
        try:
            usage_raw = await inject_slash_and_capture(b, "/usage")
            from tmuxbot.backends.claude_code import parse_cost as _pcost
            usage_summary = _pcost(usage_raw) if b.backend == "claude_code" else None
        except Exception:
            log.exception("inject /usage err")

        jl = backend.find_active_jsonl(b)
        stats = backend.aggregate_usage(jl, last_n=500) if jl else None

        parts = [
            f"ℹ️ <b>综合状态</b>  · <code>{html.escape(b.name)}</code>",
            "",
            "🔌 <b>一、连接状态</b>",
            f"<pre>{html.escape(conn_table)}</pre>",
        ]
        if ctx_summary:
            parts += ["", "📊 <b>二、上下文用量</b>", ctx_summary]
        if usage_summary:
            parts += ["", "💰 <b>三、用量与花费</b>", usage_summary]
        if stats:
            sess_rows = [
                ["📥 输入 token", f"{stats['input']:,}"],
                ["📤 输出 token", f"{stats['output']:,}"],
                ["📦 缓存创建", f"{stats['cache_create']:,}"],
                ["📦 缓存命中", f"{stats['cache_read']:,}"],
                ["🎯 缓存命中率", f"{stats['cache_hit_rate']*100:.1f}%"],
                ["💬 助手回复", f"{stats['count']} 条"],
            ]
            parts += [
                "", "📈 <b>四、本会话累计 (jsonl)</b>",
                f"<pre>{html.escape(render_table(['项目', '值'], sess_rows))}</pre>",
            ]

        await self.send_html(chat_id, thread_id, "\n".join(parts))

    async def send_light_status_summary(
        self, b: "Binding", chat_id: int | str, thread_id: int | None
    ) -> None:
        from tmuxbot.tmux import _is_tui_busy, tmux_has_session, tmux_pane_command

        alive = tmux_has_session(b.tmux_session)
        raw = tmux_capture(b.tmux_target, 12) if alive else ""
        pane_cmd = tmux_pane_command(b.tmux_target) if alive else "-"
        screen_footer = screen_footer_from_capture(raw) or "-"
        rows = [
            ["tmux", "正常" if alive else "断开", b.tmux_target],
            ["状态", "工作中" if alive and _is_tui_busy(raw) else "空闲", screen_footer],
            ["pane", pane_cmd, str(b.cwd)],
        ]
        body = "\n".join(
            [
                f"ℹ️ <b>轻状态</b> · <code>{html.escape(b.name)}</code>",
                f"<pre>{html.escape(render_table(['项目', '状态', '详情'], rows))}</pre>",
            ]
        )
        await self.send_html(chat_id, thread_id, body)

    async def send_interrupt_confirmation(
        self, b: "Binding", chat_id: int | str, thread_id: int | None
    ) -> Any:
        token = binding_token(b.name)
        markup = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="确认中断", callback_data=f"tui:{token}:ctrl_c"),
                    InlineKeyboardButton(text="取消", callback_data=f"tui:{token}:refresh"),
                ]
            ]
        )
        return await self._tg_call(
            lambda: self.bot.send_message(
                int(chat_id),
                "确认发送 Ctrl-C 强制中断当前 TUI？",
                message_thread_id=thread_id,
                reply_markup=markup,
            )
        )

    # ────────── retry / 出站 ──────────
    async def _tg_call(self, fn: Callable, max_retries: int = 4) -> Any:
        for i in range(max_retries):
            try:
                return await fn()
            except TelegramRetryAfter as e:
                log.warning(f"flood wait {e.retry_after}s")
                await asyncio.sleep(e.retry_after + 0.5)
            except TelegramBadRequest as e:
                log.warning(f"bad request (no retry): {e}")
                return None
            except TelegramNetworkError as e:
                log.warning(f"net err (try {i + 1}): {e}")
                await asyncio.sleep(2 ** i)
            except Exception as e:
                log.warning(f"send err (try {i + 1}): {e}")
                await asyncio.sleep(2 ** i)
        log.error("send giving up")
        return None

    async def send_html(self, chat_id: int, thread_id: int | None, html_text: str) -> Any:
        """单条 HTML, 长则分片或转 .txt 附件。返回第一条 message 对象 (供后续 edit)"""
        if utf16_len(html_text) > TG_DOC_THRESHOLD:
            try:
                plain = re.sub(r"<[^>]+>", "", html_text)
                file = BufferedInputFile(plain.encode("utf-8"), filename="output.txt")
                return await self._tg_call(
                    lambda: self.bot.send_document(
                        chat_id, file, caption="(long output)", message_thread_id=thread_id
                    )
                )
            except Exception as e:
                log.exception(f"send_document fallback to chunks: {e}")
        first_msg = None
        for chunk in split_for_tg(html_text):
            msg = await self._tg_call(
                lambda c=chunk: self.bot.send_message(chat_id, c, message_thread_id=thread_id)
            )
            if first_msg is None:
                first_msg = msg
        return first_msg

    async def edit_html(self, chat_id: int, message_id: int, html_text: str) -> None:
        """编辑已发送消息 — 工具调用聚合用。超长直接 truncate 末尾。"""
        if utf16_len(html_text) > TG_SPLIT:
            html_text = html_text[: TG_SPLIT - 30] + "\n<i>… (内容已截断)</i>"
        await self._tg_call(
            lambda: self.bot.edit_message_text(
                chat_id=chat_id, message_id=message_id, text=html_text,
            )
        )

    async def send_pre(self, chat_id: int, thread_id: int | None, raw_text: str) -> None:
        if not raw_text.strip():
            return
        clean_text, attachments = split_outbound_attachments(raw_text)
        if clean_text.strip() and utf16_len(clean_text) > TG_DOC_THRESHOLD:
            try:
                file = BufferedInputFile(clean_text.encode("utf-8"), filename="capture.txt")
                await self._tg_call(
                    lambda: self.bot.send_document(
                        chat_id, file, caption="📷 capture (long)",
                        message_thread_id=thread_id,
                    )
                )
                return
            except Exception as e:
                log.exception(f"send_document fallback: {e}")
        if clean_text.strip():
            escaped = html.escape(clean_text)
            for chunk in split_for_tg(escaped, limit=TG_SPLIT - 12):
                wrapped = f"<pre>{chunk}</pre>"
                await self._tg_call(
                    lambda c=wrapped: self.bot.send_message(
                        chat_id, c, message_thread_id=thread_id
                    )
                )
        for attachment in attachments:
            if attachment.kind == "image":
                await self.send_image(chat_id, thread_id, attachment.path)
            else:
                await self.send_file(chat_id, thread_id, attachment.path)

    async def send_image(
        self, chat_id: int | str, thread_id: int | None, path: str | Path,
        caption: str | None = None,
    ) -> Any:
        file = FSInputFile(path)
        return await self._tg_call(
            lambda: self.bot.send_photo(
                int(chat_id), file, caption=caption, message_thread_id=thread_id
            )
        )

    async def send_file(
        self, chat_id: int | str, thread_id: int | None, path: str | Path,
        caption: str | None = None,
    ) -> Any:
        file = FSInputFile(path)
        return await self._tg_call(
            lambda: self.bot.send_document(
                int(chat_id), file, caption=caption, message_thread_id=thread_id
            )
        )

    async def send_assistant_reply(self, b: "Binding", envelope: ReplyEnvelope) -> Any:
        footer_text = self.backend.format_status_footer(envelope.footer)
        rendered = render_assistant_reply(
            b,
            envelope,
            full_output_threshold=TG_REPLY_FULL_OUTPUT_THRESHOLD,
            footer_text=footer_text,
        )
        token = binding_token(b.name)
        action_buttons = {
            "screen": InlineKeyboardButton(text="屏幕", callback_data=f"tui:{token}:refresh"),
            "status": InlineKeyboardButton(text="状态", callback_data=f"tui:{token}:status"),
            "cancel": InlineKeyboardButton(text="取消", callback_data=f"tui:{token}:esc"),
            "interrupt": InlineKeyboardButton(
                text="强制中断", callback_data=f"tui:{token}:confirm_ctrl_c"
            ),
        }
        buttons = [action_buttons[action] for action in envelope.actions if action in action_buttons]
        markup = InlineKeyboardMarkup(inline_keyboard=[buttons]) if buttons else None
        first_msg = None
        for chunk in split_for_tg(rendered.chat_html):
            msg = await self._tg_call(
                lambda c=chunk: self.bot.send_message(
                    int(b.chat_id),
                    c,
                    message_thread_id=b.thread_id,
                    reply_markup=markup if first_msg is None else None,
                )
            )
            if first_msg is None:
                first_msg = msg

        if rendered.full_text:
            file = BufferedInputFile(
                rendered.full_text.encode("utf-8"),
                filename=f"assistant-{safe_filename_fragment(b.name)}.txt",
            )
            await self._tg_call(
                lambda: self.bot.send_document(
                    int(b.chat_id),
                    file,
                    caption="完整输出",
                    message_thread_id=b.thread_id,
                )
            )

        for attachment in envelope.attachments:
            if is_image_file(attachment):
                await self.send_image(b.chat_id, b.thread_id, attachment)
            else:
                await self.send_file(b.chat_id, b.thread_id, attachment)
        return first_msg

    async def send_chat_action(self, chat_id: int, thread_id: int | None, action: str) -> None:
        try:
            await self.bot.send_chat_action(
                chat_id=chat_id, action=action, message_thread_id=thread_id,
            )
        except Exception as e:
            log.debug(f"send_chat_action err: {e}")

    async def send_picker_card(
        self, chat_id: int, thread_id: int | None,
        body_html: str, binding_name: str, num_options: int = 9,
    ) -> Any:
        """picker 卡片 + 1-9 数字按钮 + ⎋ 取消。callback_data 格式: 'picker:<binding>:<n>'"""
        rows: list[list[InlineKeyboardButton]] = []
        row: list[InlineKeyboardButton] = []
        for n in range(1, num_options + 1):
            row.append(InlineKeyboardButton(
                text=str(n), callback_data=f"picker:{binding_name}:{n - 1}",
            ))
            if len(row) == 3:
                rows.append(row); row = []
        if row:
            rows.append(row)
        rows.append([InlineKeyboardButton(
            text="⎋ 取消 picker", callback_data=f"picker:{binding_name}:esc",
        )])
        markup = InlineKeyboardMarkup(inline_keyboard=rows)
        return await self._tg_call(lambda: self.bot.send_message(
            chat_id, body_html, message_thread_id=thread_id, reply_markup=markup,
        ))

    async def send_interaction_card(
        self, chat_id: int, thread_id: int | None, html_text: str, binding_name: str
    ) -> Any:
        token = binding_token(binding_name)
        rows: list[list[InlineKeyboardButton]] = []
        semantic_actions = semantic_actions_from_body(html_text)
        if semantic_actions:
            rows.append(
                [
                    InlineKeyboardButton(
                        text=action.label,
                        callback_data=f"tui:{token}:sem:{action.action}",
                    )
                    for action in semantic_actions[:3]
                ]
            )
        rows.extend([
            [
                InlineKeyboardButton(text="↑", callback_data=f"tui:{token}:up"),
            ],
            [
                InlineKeyboardButton(text="←", callback_data=f"tui:{token}:left"),
                InlineKeyboardButton(text="↵", callback_data=f"tui:{token}:enter"),
                InlineKeyboardButton(text="→", callback_data=f"tui:{token}:right"),
            ],
            [
                InlineKeyboardButton(text="↓", callback_data=f"tui:{token}:down"),
                InlineKeyboardButton(text="Tab", callback_data=f"tui:{token}:tab"),
                InlineKeyboardButton(text="Space", callback_data=f"tui:{token}:space"),
            ],
            [
                InlineKeyboardButton(text="⎋", callback_data=f"tui:{token}:esc"),
                InlineKeyboardButton(text="刷新", callback_data=f"tui:{token}:refresh"),
            ],
        ])
        markup = InlineKeyboardMarkup(inline_keyboard=rows)
        return await self._tg_call(
            lambda: self.bot.send_message(
                chat_id, html_text, message_thread_id=thread_id, reply_markup=markup
            )
        )

    # ────────── ACL ──────────
    def _acl_ok(self, m: Message) -> bool:
        """全局 ACL: from_user 在白名单 **且** source (chat_id, thread_id) 已配置 binding。
        未配置的 source 即使 Boss 本人发也一律静默 — 不打 👀 / 不 typing / 不回复 / 不警告。"""
        if self.state.setup_mode:
            return True
        if not m.from_user or m.from_user.id != self.state.boss_user_id:
            return False
        if not self._message_allowed_by_mention(m):
            return False
        return self.find_binding(*source_key(m)) is not None

    def _message_allowed_by_mention(self, m: Message) -> bool:
        incoming = self.normalize_incoming(m)
        return incoming_message_is_addressed(
            incoming, require_addressing=self.group_only_when_mentioned
        )

    async def _resolve_binding_or_reply(self, m: Message) -> "Binding | None":
        if not self._acl_ok(m):
            return None
        return self.find_binding(*source_key(m))  # ACL 已保证非 None

    # ────────── setup mode (首次锁定 user_id + chat_id) ──────────
    def _save_env_user_id(self, user_id: int) -> None:
        txt = self.env_file.read_text()
        if re.search(r"^BOSS_USER_ID=.*$", txt, flags=re.M):
            txt = re.sub(r"^BOSS_USER_ID=.*$", f"BOSS_USER_ID={user_id}", txt, flags=re.M)
        else:
            txt += f"\nBOSS_USER_ID={user_id}\n"
        self.env_file.write_text(txt)

    def _save_binding_chat_id(self, name: str, chat_id: int) -> None:
        try:
            raw = yaml.safe_load(self.bindings_file.read_text()) or {}
            for b in raw.get("bindings", []):
                if b.get("name") == name:
                    b["chat_id"] = chat_id
                    break
            self.bindings_file.write_text(
                yaml.safe_dump(raw, allow_unicode=True, sort_keys=False)
            )
        except Exception as e:
            log.exception(f"save_binding_chat_id err: {e}")

    async def _do_setup(self, m: Message) -> "Binding | None":
        if not self.state.setup_mode:
            return None
        if m.chat.type != "private":
            await m.reply("Setup 模式: 请用 DM 发消息触发首次绑定")
            return None
        if not m.from_user:
            return None
        uid = m.from_user.id
        log.info(f"SETUP: locking BOSS_USER_ID={uid}, chat_id={m.chat.id}")
        self._save_env_user_id(uid)
        target = next(
            (b for b in self.state.bindings if b.chat_id == 0 and b.thread_id is None), None
        )
        if target:
            self._save_binding_chat_id(target.name, m.chat.id)
            target.chat_id = m.chat.id
            log.info(f"SETUP: binding '{target.name}' chat_id locked to {m.chat.id}")
        self.state.boss_user_id = uid
        self.state.setup_mode = False
        await m.reply(
            "✅ Setup 完成\n"
            f"user_id=<code>{uid}</code>\n"
            f"绑定: <code>{target.name if target else 'none'}</code>\n"
            f"tmux=<code>{html.escape(target.tmux_target) if target else '-'}</code>\n"
            "现在可以直接发消息了, Claude 会在 tmux 里收到"
        )
        return target

    # ────────── handlers 注册 ──────────
    def _register_handlers(self) -> None:
        dp = self.dp
        S = self.state
        F_ = self  # frontend self alias 给闭包

        # ─── ack middleware (👀 反应 + typing) ─────────
        @dp.message.middleware()
        async def ack_received(handler, event: Message, data):
            # ★ ACL 全局规则: Boss 白名单 + source 必须有 binding 才 ack;
            # 未配置 source 直接 pass 到 handler (handler 内 _acl_ok 也会拒)
            if (
                not S.setup_mode
                and event.from_user
                and event.from_user.id == S.boss_user_id
                and F_._message_allowed_by_mention(event)
            ):
                tid = thread_id_of(event)
                b = F_.find_binding(event.chat.id, tid)
                if b is not None:
                    try:
                        await event.bot.set_message_reaction(
                            chat_id=event.chat.id,
                            message_id=event.message_id,
                            reaction=[ReactionTypeEmoji(emoji=ACK_REACTION)],
                        )
                    except Exception as e:
                        log.debug(f"set_message_reaction err: {e}")
                    try:
                        await event.bot.send_chat_action(
                            chat_id=event.chat.id, action="typing", message_thread_id=tid,
                        )
                    except Exception as e:
                        log.debug(f"send_chat_action err: {e}")
                    S.last_active[b.name] = time.time()
            return await handler(event, data)

        # ─── /whoami ─────────
        @dp.message(Command("whoami"))
        async def cmd_whoami(m: Message):
            if not m.from_user:
                return
            await m.reply(
                f"user_id=<code>{m.from_user.id}</code>\n"
                f"chat_id=<code>{m.chat.id}</code>\n"
                f"thread_id=<code>{m.message_thread_id}</code>\n"
                f"type=<code>{m.chat.type}</code>"
            )

        # ─── /status (跟 backend 紧耦合, 走 inject_slash_and_capture) ─────────
        @dp.message(Command("status"))
        async def cmd_status(m: Message):
            if not F_._acl_ok(m):
                return
            b = F_.find_binding(*source_key(m))
            # ACL 已保证 b 非 None (未配置 source 在 _acl_ok 就已拒)
            assert b is not None
            notice = await m.reply("⏳ 抓综合状态…(注入 /context + /usage,可能短暂中断生成)")
            await F_.send_status_summary(b, m.chat.id, thread_id_of(m))
            try:
                await notice.delete()
            except Exception:
                pass

        # ─── /esc /cc /eof ─────────
        async def _send_key(m: Message, key: str, label: str):
            b = await F_._resolve_binding_or_reply(m)
            if not b:
                return
            tmux_send_key(b.tmux_target, key)
            await m.reply(label)

        @dp.message(Command("esc"))
        async def cmd_esc(m: Message):
            await _send_key(m, "Escape", "⎋ Escape")

        @dp.message(Command("cc"))
        async def cmd_cc(m: Message):
            await _send_key(m, "C-c", "⌃C")

        @dp.message(Command("eof"))
        async def cmd_eof(m: Message):
            await _send_key(m, "C-d", "⌃D")

        # ─── /screen ─────────
        @dp.message(Command("screen"))
        async def cmd_screen(m: Message):
            b = await F_._resolve_binding_or_reply(m)
            if not b:
                return
            out = tmux_capture(b.tmux_target, 60)
            await F_.send_pre(m.chat.id, thread_id_of(m), out)

        # ─── /info ─────────
        @dp.message(Command("info"))
        async def cmd_info(m: Message):
            b = await F_._resolve_binding_or_reply(m)
            if not b:
                return
            backend = F_.backend
            jl = backend.find_active_jsonl(b)
            if not jl:
                await m.reply("📊 没找到 jsonl 文件")
                return
            stats = backend.aggregate_usage(jl, last_n=500)
            if not stats:
                await m.reply("📊 jsonl 里还没有 assistant 数据")
                return

            def fmt(n: int) -> str:
                return f"{n:,}"

            total_in = stats["input"] + stats["cache_create"] + stats["cache_read"]
            parts = [
                f"📊 <b>会话累计统计</b>  · {b.name}",
                f"📨 助手回复 <b>{stats['count']}</b> 条",
            ]
            if stats.get("model"):
                parts.append(f"🧠 当前模型 <code>{html.escape(stats['model'])}</code>")
            parts += [
                "",
                f"📥 计费输入合计 <code>{fmt(total_in)}</code>",
                f"   ├ 新输入 <code>{fmt(stats['input'])}</code>",
                f"   ├ 缓存创建 <code>{fmt(stats['cache_create'])}</code>",
                f"   └ 缓存命中 <code>{fmt(stats['cache_read'])}</code>",
                f"📤 输出 token <code>{fmt(stats['output'])}</code>",
                "",
                f"🎯 <b>缓存命中率 {stats['cache_hit_rate'] * 100:.1f}%</b>",
            ]
            if stats.get("last_ts"):
                parts.append(f"⏱ 最近回复 <code>{html.escape(stats['last_ts'])}</code>")
            await m.reply("\n".join(parts))

        # ─── /restart ─────────
        @dp.message(Command("restart"))
        async def cmd_restart(m: Message):
            b = await F_._resolve_binding_or_reply(m)
            if not b:
                return
            backend = F_.backend
            tmux_send_key(b.tmux_target, "C-c")
            await asyncio.sleep(0.5)
            tmux_send_key(b.tmux_target, "C-d")
            await asyncio.sleep(2.0)
            await ensure_binding_running(
                backend, b, self.state, reason="telegram-restart", wait=True
            )
            await m.reply(f"🔄 已 restart {backend.name}")

        # ─── 文件 / 图片 ─────────
        @dp.message(F.photo | F.document | F.video | F.animation | F.audio | F.voice)
        async def on_file(m: Message):
            from tmuxbot.tmux import tmux_send_text

            if S.setup_mode:
                await F_._do_setup(m)
                return
            b = await F_._resolve_binding_or_reply(m)
            if not b:
                return
            attachment = _message_attachment(m)
            if attachment is None:
                return
            file_id, fname, file_size, kind = attachment
            if file_size and file_size > MAX_FILE_MB * 1024 * 1024:
                await m.reply(f"文件超过 {MAX_FILE_MB}MB")
                return
            save_path = attachment_path(
                "telegram", m.message_id, file_id[-16:], fname
            )
            try:
                f = await m.bot.get_file(file_id)
                await m.bot.download_file(f.file_path, destination=save_path)
            except Exception as e:
                await m.reply(f"❌ 下载失败: {html.escape(str(e))}")
                return
            ref = attachment_ref(
                save_path,
                kind="image" if kind == "photo" else "file",
                name=fname,
            )
            incoming = F_.normalize_incoming(m, attachments=(ref,))
            default_caption = "请处理这个图片" if ref.kind == "image" else "请处理这个文件"
            inject = attachment_prompt(
                incoming.text,
                [item.path for item in incoming.attachments],
                default_caption=default_caption,
                backend_name=F_.backend.name,
            )
            backend = F_.backend
            await ensure_binding_running(
                backend, b, self.state, reason="telegram-file", wait=True
            )
            await tmux_send_text(b.tmux_target, inject)
            await m.reply(f"📎 已注入 <code>{html.escape(str(save_path))}</code>")

        # ─── forum topic 名缓存 (服务消息, 只缓存不干别的) ─────────
        # F.forum_topic_created / _edited 是独立 filter, 普通文本不匹配 → 不吞正常消息流。
        @dp.message(F.forum_topic_created)
        async def on_topic_created(m: Message):
            if m.message_thread_id is not None and m.forum_topic_created:
                F_._topic_names[(m.chat.id, m.message_thread_id)] = (
                    m.forum_topic_created.name
                )

        @dp.message(F.forum_topic_edited)
        async def on_topic_edited(m: Message):
            # edited 可能只改名; .name 在改名时才有值
            if (
                m.message_thread_id is not None
                and m.forum_topic_edited
                and m.forum_topic_edited.name
            ):
                F_._topic_names[(m.chat.id, m.message_thread_id)] = (
                    m.forum_topic_edited.name
                )

        # ─── /init 自动开通 (未绑定 source + Boss) ─────────
        # 注册在 F.text 之前: Boss 在**未绑定** chat 发 /init → 建会话。
        # ACL 特殊性: 此场景 source 尚无 binding, 不能走 _acl_ok (它要求 source 已绑定),
        # 只校验 from_user 是 Boss; 已绑定的 source 发 /init 直接忽略 (provision_chat 防重复)。
        @dp.message(Command("init"))
        async def cmd_init(m: Message):
            from tmuxbot.provision import AsciiDirRequired, provision_chat

            if S.setup_mode:
                return
            if not m.from_user or m.from_user.id != S.boss_user_id:
                return  # 非 Boss → 静默
            if not F_._message_allowed_by_mention(m):
                return
            tid = thread_id_of(m)
            if F_.find_binding(m.chat.id, tid) is not None:
                return  # 已绑定 → 交给普通文本流, 这里静默 (避免重复开通)
            # 取名优先级: 话题名 (forum topic) > 群名 > full_name > tg-<id>。
            # 话题里 m.chat.title 是群名, 话题名只在缓存里 (forum_topic_created 服务消息)。
            _topic_missing = False
            if m.message_thread_id is not None:
                _topic = F_._topic_names.get((m.chat.id, m.message_thread_id))
                if _topic:
                    display_name = _topic
                else:
                    _topic_missing = True  # 没缓存到 → 退回群名, 确认消息里提示
                    display_name = m.chat.title or f"tg-{m.chat.id}"
            else:
                display_name = (
                    m.chat.title
                    or getattr(m.chat, "full_name", None)
                    or f"tg-{m.chat.id}"
                )
            # /init <目录名> → 用指定目录; /init → 用群名新建
            _parts = (m.text or "").strip().split(maxsplit=1)
            _arg = _parts[1].strip() if len(_parts) > 1 else None
            try:
                b = await provision_chat(
                    F_, S,
                    chat_id=m.chat.id,
                    thread_id=tid,
                    display_name=display_name,
                    offsets_file=F_.offsets_file,
                    bindings_file=F_.bindings_file,
                    bot_token_env=F_.bot_token_env,
                    project_base=F_.project_base,
                    channel="telegram",
                    target_dir=_arg,
                )
            except AsciiDirRequired:
                await m.reply(
                    "⚠️ <b>群/话题名含中文,项目目录需英文</b>\n"
                    "请用 <code>/init &lt;英文目录名&gt;</code> 指定 (tmux 仍用名字)\n"
                    "或 /projects 看现有目录"
                )
                return
            if b is None:
                await m.reply(
                    "❌ <b>开通会话失败</b>\n请检查日志或手动配置 bindings.yaml"
                )
                return
            F_._cancel_unknown_chat_leave(m.chat.id)
            _tip = ""
            if _topic_missing and not _arg:
                _tip = (
                    "\n⚠️ 未取到话题名 (用了群名), "
                    "可 <code>/init &lt;名&gt;</code> 指定"
                )
            await m.reply(
                f"✅ <b>已开通会话</b>\n名称: {html.escape(b.name)}\n"
                f"目录: <code>{html.escape(str(b.cwd))}</code>\n现在可以直接对话了"
                f"{_tip}"
            )

        # ─── /deinit 手动拆除当前 source 的 binding (Boss; 已绑定群/话题) ───
        # 复用 provision.deprovision_chat: 注销 binding + 杀 tmux + 删 yaml 条目,
        # 项目目录/jsonl 保留 (可重新 /init 接回)。
        # source 含 thread_id, 话题精确匹配该话题的 binding (find_binding 本就只查本前端子集)。
        @dp.message(Command("deinit"))
        async def cmd_deinit(m: Message):
            from tmuxbot.provision import deprovision_chat

            if S.setup_mode:
                return
            if not m.from_user or m.from_user.id != S.boss_user_id:
                return  # 非 Boss → 静默
            if not F_._message_allowed_by_mention(m):
                return
            b = F_.find_binding(*source_key(m))
            if b is None:
                await m.reply("本群/话题未绑定,无需拆除")
                return
            _name = b.name
            await deprovision_chat(F_, S, b, bindings_file=F_.bindings_file)
            await m.reply(
                f"✅ 已拆除会话「{html.escape(_name)}」\n"
                "tmux 已关 · binding 注销\n"
                "项目目录和历史 jsonl 保留(可重新 /init 接回)"
            )

        # ─── /projects 列 base 下现有目录 (Boss; 绑定/未绑定群都能用, 纯信息) ───
        @dp.message(Command("projects"))
        async def cmd_projects(m: Message):
            if S.setup_mode:
                return
            if not m.from_user or m.from_user.id != S.boss_user_id:
                return  # 非 Boss → 静默
            if not F_._message_allowed_by_mention(m):
                return
            await m.reply(F_._list_projects())

        # ─── 文本 ─────────
        @dp.message(F.text)
        async def on_text(m: Message):
            from tmuxbot.dispatch import dispatch_incoming_text

            if S.setup_mode:
                await F_._do_setup(m)
                return
            b = await F_._resolve_binding_or_reply(m)
            if not b:
                return
            incoming = F_.normalize_incoming(m)
            # ★ bot_username 传入供 dispatch 剥 @bot_username 后缀
            # (TG group 内命令自动带 /compact@ztl_claude_bot 形式)
            # _bot_username 在 start_polling 时通过 get_me() 填入, 避免每条消息都 API 请求
            await dispatch_incoming_text(
                F_, F_.backend, b, S,
                incoming.source_id, incoming.thread_id, incoming.text,
                bot_username=F_._bot_username,
            )

        # ─── picker callback ─────────
        @dp.callback_query(F.data.startswith("picker:"))
        async def on_picker_callback(cq: CallbackQuery):
            from tmuxbot.picker import extract_picker_block

            if S.setup_mode:
                await cq.answer("⚠️ setup 中"); return
            if cq.from_user and cq.from_user.id != S.boss_user_id:
                await cq.answer("⚠️ 无权限"); return
            # ★ 全局 ACL 双重门禁: source 没在本 frontend 的 binding 子集 → 静默
            if cq.message:
                cq_tid = getattr(cq.message, "message_thread_id", None)
                if F_.find_binding(cq.message.chat.id, cq_tid) is None:
                    await cq.answer(); return
            parts = (cq.data or "").split(":", 2)
            if len(parts) != 3:
                await cq.answer("⚠️ 格式错误"); return
            _, b_name, action = parts
            b = next((bb for bb in S.bindings if bb.name == b_name), None)
            if not b:
                await cq.answer("⚠️ binding 未找到"); return

            pre_block = extract_picker_block(tmux_capture(b.tmux_target, 80))
            if pre_block is None:
                await cq.answer("⚠️ 屏幕上 picker 已消失,此卡片过时", show_alert=True)
                try:
                    await cq.message.edit_text(
                        (cq.message.html_text or "") + "\n\n<i>⚠ picker 已消失,按钮无效</i>",
                    )
                except Exception:
                    pass
                return

            if action == "esc":
                tmux_send_key(b.tmux_target, "Escape")
                op_label = "⎋ Escape"
            else:
                try:
                    idx = int(action)
                except ValueError:
                    await cq.answer("⚠️ 参数错误"); return
                for _ in range(idx):
                    tmux_send_key(b.tmux_target, "Down")
                    await asyncio.sleep(0.05)
                await asyncio.sleep(0.1)
                tmux_send_key(b.tmux_target, "Enter")
                op_label = f"选项 {idx + 1}"

            await asyncio.sleep(0.6)
            post_block = extract_picker_block(tmux_capture(b.tmux_target, 80))
            if post_block is None:
                await cq.answer(f"✓ {op_label} 已生效")
                mark = f"<b>✓ {op_label} 已生效</b>"
                S.picker_notified.pop(b.name, None)
            else:
                await cq.answer(f"⚠ 已发 {op_label},picker 仍在屏幕上", show_alert=True)
                mark = f"<i>⚠ 已发 {op_label},但 picker 仍在</i>"

            try:
                await cq.message.edit_text((cq.message.html_text or "") + f"\n\n{mark}")
            except Exception:
                pass

        # ─── generic TUI interaction callback ─────────
        @dp.callback_query(F.data.startswith("tui:"))
        async def on_tui_callback(cq: CallbackQuery):
            if S.setup_mode:
                await cq.answer("⚠️ setup 中")
                return
            if cq.from_user and cq.from_user.id != S.boss_user_id:
                await cq.answer("⚠️ 无权限")
                return
            if cq.message:
                cq_tid = getattr(cq.message, "message_thread_id", None)
                if F_.find_binding(cq.message.chat.id, cq_tid) is None:
                    await cq.answer()
                    return
            parts = (cq.data or "").split(":")
            if len(parts) < 3:
                await cq.answer("⚠️ 格式错误")
                return
            _, token, *action_parts = parts
            if action_parts[0] == "sem" and len(action_parts) == 2:
                action = action_parts[1]
                is_semantic = True
            elif len(action_parts) == 1:
                action = action_parts[0]
                is_semantic = False
            else:
                await cq.answer("⚠️ 格式错误")
                return
            b = binding_by_token(F_.bindings, token)
            if not b:
                await cq.answer("⚠️ binding 未找到")
                return
            if cq.message is None:
                await cq.answer("⚠️ 消息不存在")
                return
            if is_semantic:
                await handle_semantic_action(
                    F_, b, cq.message.chat.id, getattr(cq.message, "message_thread_id", None), action
                )
            elif action == "status":
                await F_.send_light_status_summary(
                    b, cq.message.chat.id, getattr(cq.message, "message_thread_id", None)
                )
            elif action == "confirm_ctrl_c":
                await F_.send_interrupt_confirmation(
                    b, cq.message.chat.id, getattr(cq.message, "message_thread_id", None)
                )
            else:
                await handle_tui_action(
                    F_, b, cq.message.chat.id, getattr(cq.message, "message_thread_id", None), action
                )
            await cq.answer("✓")

        # ─── 成员变更: 非白名单群自动 leave / 已绑定群被移除→拆除会话 ───
        @dp.my_chat_member()
        async def on_membership(ev):
            from tmuxbot.provision import deprovision_chat

            if S.setup_mode:
                return
            chat_id = ev.chat.id
            # bot 自己的新状态: left / kicked = 被移出群 (或群被删)
            new_status = getattr(
                getattr(ev, "new_chat_member", None), "status", None
            )
            removed = new_status in ("left", "kicked")
            actor_user_id = getattr(getattr(ev, "from_user", None), "id", None)
            if removed:
                F_._cancel_unknown_chat_leave(chat_id)

            # 该 chat 在本 frontend 是否有 binding (含 forum topic 的 thread)
            bound = [b for b in F_.bindings if b.chat_id == chat_id]
            grace_unknown = should_grace_unknown_chat(
                setup_mode=S.setup_mode,
                bound_count=len(bound),
                removed=removed,
            )

            if removed and bound:
                # 已绑定群被移除 → 拆除该 chat 下所有 binding (group 可能有多个 topic)
                log.info(f"bot removed from bound chat {chat_id}, 拆除 {len(bound)} binding")
                for b in list(bound):
                    try:
                        await deprovision_chat(
                            F_, S, b, bindings_file=F_.bindings_file
                        )
                    except Exception:
                        log.exception(f"deprovision {b.name} err")
                return

            # 未绑定且 bot 仍在群里: 给 Boss 60s 发送 /init; 到期仍未绑定才自动退群。
            if grace_unknown:
                F_._cancel_unknown_chat_leave(chat_id)
                log.info(
                    "stayed in unbound chat %s invited by user %s; waiting %.0fs for /init",
                    chat_id,
                    actor_user_id,
                    UNKNOWN_CHAT_INIT_GRACE_SECONDS,
                )
                try:
                    await ev.bot.send_message(
                        chat_id,
                        "已进入群组。请在 60 秒内发送 /init 开通 tmux 会话; "
                        "如果群/话题名含中文, 用 /init <英文目录名>。"
                        "超时未开通会自动退出。",
                    )
                except Exception as e:
                    log.debug(f"send unknown chat init hint err: {e}")

                async def _leave_if_still_unbound() -> None:
                    try:
                        await asyncio.sleep(UNKNOWN_CHAT_INIT_GRACE_SECONDS)
                        if any(b.chat_id == chat_id for b in F_.bindings):
                            return
                        log.warning(
                            "left unbound chat %s after %.0fs init grace",
                            chat_id,
                            UNKNOWN_CHAT_INIT_GRACE_SECONDS,
                        )
                        try:
                            await ev.bot.leave_chat(chat_id)
                        except Exception as e:
                            log.debug(f"delayed leave unknown chat err: {e}")
                    except asyncio.CancelledError:
                        raise
                    finally:
                        F_._unknown_chat_leave_tasks.pop(chat_id, None)

                F_._unknown_chat_leave_tasks[chat_id] = asyncio.create_task(
                    _leave_if_still_unbound()
                )
                return

    # ────────── 启动 / 停止 ──────────
    async def start_polling(self) -> None:
        try:
            me = await self.bot.get_me()
            self._bot_username = me.username
            self._bot_id = me.id
        except Exception as e:
            log.warning(f"get_me err: {e}")
        try:
            await self.bot.set_my_commands(
                [BotCommand(command=c, description=d) for c, d in self.backend.bot_commands]
            )
        except Exception as e:
            log.warning(f"set_my_commands err: {e}")
        await self.dp.start_polling(self.bot, allowed_updates=self.dp.resolve_used_update_types())

    async def stop(self) -> None:
        for chat_id in list(self._unknown_chat_leave_tasks):
            self._cancel_unknown_chat_leave(chat_id)
        await self.dp.stop_polling()
        await self.bot.session.close()
