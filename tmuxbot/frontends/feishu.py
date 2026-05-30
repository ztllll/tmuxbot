"""飞书前端: lark-oapi WebSocket 长连接 + interactive card 发送/编辑。

每个实例 = 一个 app_id/app_secret + 一个 backend + 一组 bindings 子集。
与 TelegramFrontend 完全平行: ACL 双重门禁 + _resolve_binding + on_message handler。

飞书 ACL:
  - sender open_id 在 Boss 白名单 (boss_open_ids)
  - (chat_id, None) 在本 frontend 的 bindings 子集

飞书无 typing 状态 API → send_chat_action 为 no-op。
发可编辑消息必须用 interactive card (text 消息不能 PATCH 编辑)。

依赖: lark-oapi>=1.4 (可选, 没装时 FeishuFrontend 实例化会 ImportError + 清晰报错)
"""
from __future__ import annotations

import asyncio
import html as html_mod
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from tmuxbot.backends.base import Backend
    from tmuxbot.state import Binding, State

log = logging.getLogger("tmuxbot")

# ────────── lark-oapi lazy import ──────────
# 没装 lark-oapi 时抛清晰 ImportError, 不影响其他前端启动
def _get_lark():
    try:
        import lark_oapi as lark
        return lark
    except ImportError:
        raise ImportError(
            "飞书前端需要 lark-oapi>=1.4, 请先安装: pip install lark-oapi"
        )


# ────────── HTML → 飞书 Markdown 转换 ──────────

# 实体反转义映射
_HTML_ENTITIES = {
    "&amp;": "&",
    "&lt;": "<",
    "&gt;": ">",
    "&quot;": '"',
    "&#39;": "'",
}


def _html_to_feishu_md(s: str) -> str:
    """把 tmuxbot 内部 Telegram HTML 转成飞书 Markdown。

    tmuxbot 内部产出的标签集:
      <b>x</b>       → **x**
      <i>x</i>       → *x*
      <s>x</s>       → ~~x~~
      <code>x</code> → `x`
      <pre>x</pre>   → ```\\nx\\n```
      &lt; &gt; &amp; → 反转义为原字符
    """
    # 先处理 <pre> (多行代码块), 避免内层标签被替换
    s = re.sub(r"<pre>(.*?)</pre>", lambda m: "```\n" + m.group(1) + "\n```", s, flags=re.DOTALL)
    # 行内标签
    s = re.sub(r"<b>(.*?)</b>", lambda m: "**" + m.group(1) + "**", s, flags=re.DOTALL)
    s = re.sub(r"<strong>(.*?)</strong>", lambda m: "**" + m.group(1) + "**", s, flags=re.DOTALL)
    s = re.sub(r"<i>(.*?)</i>", lambda m: "*" + m.group(1) + "*", s, flags=re.DOTALL)
    s = re.sub(r"<em>(.*?)</em>", lambda m: "*" + m.group(1) + "*", s, flags=re.DOTALL)
    s = re.sub(r"<s>(.*?)</s>", lambda m: "~~" + m.group(1) + "~~", s, flags=re.DOTALL)
    s = re.sub(r"<del>(.*?)</del>", lambda m: "~~" + m.group(1) + "~~", s, flags=re.DOTALL)
    s = re.sub(r"<code>(.*?)</code>", lambda m: "`" + m.group(1) + "`", s, flags=re.DOTALL)
    # 剩余标签兜底去除
    s = re.sub(r"<[^>]+>", "", s)
    # HTML 实体反转义
    for entity, char in _HTML_ENTITIES.items():
        s = s.replace(entity, char)
    return s


def _build_card(md_text: str) -> str:
    """构造飞书 interactive card JSON (update_multi=True 支持 PATCH 编辑)"""
    card = {
        "config": {
            "wide_screen_mode": True,
            "update_multi": True,
        },
        "elements": [
            {
                "tag": "markdown",
                "content": md_text or "（空）",
            }
        ],
    }
    return json.dumps(card, ensure_ascii=False)


def _make_fake_msg(message_id: str) -> Any:
    """返回带 .message_id 属性的轻量对象, 供 aggregator 后续 edit 用"""
    class _Msg:
        pass
    obj = _Msg()
    obj.message_id = message_id
    return obj


# ────────── FeishuFrontend ──────────

class FeishuFrontend:
    """飞书 bot 前端。通过 lark-oapi WebSocket 长连接收发消息。"""

    name = "feishu"

    def __init__(
        self,
        app_id: str,
        app_secret: str,
        state: "State",
        backend: "Backend",
        bindings: list["Binding"],
        boss_open_ids: list[str],           # 飞书 open_id 白名单 (对应 TG BOSS_USER_ID)
        group_only_when_mentioned: bool = True,  # 群消息仅 @bot 时才响应
        offsets_file: "Path | None" = None,      # offsets.json 路径 (auto-provision 起 tailer 用)
        bindings_file: "Path | None" = None,     # bindings.yaml 路径 (auto-provision 持久化用)
        bot_token_env: str = "FEISHU",           # 本 frontend 的 token env key (持久化写回用)
        project_base: str = os.path.expanduser("~/projects"),  # 新项目目录的父目录
    ) -> None:
        # 触发 lazy import 检查, 没装直接崩 (早于启动, 报错清晰)
        self._lark = _get_lark()

        self.app_id = app_id
        self.app_secret = app_secret
        self.state = state
        self.backend = backend
        self.bindings = bindings
        self.boss_open_ids = set(boss_open_ids)
        self.group_only_when_mentioned = group_only_when_mentioned
        self.offsets_file = offsets_file
        self.bindings_file = bindings_file
        self.bot_token_env = bot_token_env
        self.project_base = project_base

        self._main_loop: asyncio.AbstractEventLoop | None = None
        self._ws_client = None   # lark.ws.Client 实例

    # ────────── binding 查找 ──────────

    def find_binding(self, chat_id: str, thread_id: None = None) -> "Binding | None":
        """飞书 thread_id 恒为 None (不分 topic)。只在本 frontend 的 bindings 子集里找。"""
        for b in self.bindings:
            if str(b.chat_id) == str(chat_id) and b.thread_id is None:
                return b
        return None

    # ────────── ACL ──────────

    def _acl_ok(self, open_id: str, chat_id: str) -> bool:
        """双重门禁:
        1. sender open_id 在 boss_open_ids 白名单
        2. (chat_id, None) 在本 frontend 的 bindings 子集
        未配置的 source 即使 boss 本人发也一律静默。
        """
        if not open_id or open_id not in self.boss_open_ids:
            return False
        return self.find_binding(chat_id) is not None

    # ────────── 飞书 REST 发送 (同步, 在 asyncio.to_thread 里调) ──────────

    def _send_card_sync(self, chat_id: str, md_text: str) -> str | None:
        """同步发 interactive card, 返回 message_id (失败返回 None)"""
        lark = self._lark
        import lark_oapi.api.im.v1 as im_v1

        client = lark.Client.builder().app_id(self.app_id).app_secret(self.app_secret).build()
        card_json = _build_card(md_text)
        body = (
            im_v1.CreateMessageRequestBody.builder()
            .receive_id(chat_id)
            .msg_type("interactive")
            .content(card_json)
            .build()
        )
        req = (
            im_v1.CreateMessageRequest.builder()
            .receive_id_type("chat_id")
            .request_body(body)
            .build()
        )
        resp = client.im.v1.message.create(req)
        if not resp.success():
            log.warning(f"feishu send_card err: code={resp.code} msg={resp.msg}")
            return None
        return resp.data.message_id

    def _patch_card_sync(self, message_id: str, md_text: str) -> bool:
        """同步 PATCH interactive card, 返回是否成功"""
        lark = self._lark
        import lark_oapi.api.im.v1 as im_v1

        client = lark.Client.builder().app_id(self.app_id).app_secret(self.app_secret).build()
        card_json = _build_card(md_text)
        # PATCH 接口只更新 content (不接受 msg_type, builder 也没这属性)
        body = (
            im_v1.PatchMessageRequestBody.builder()
            .content(card_json)
            .build()
        )
        req = (
            im_v1.PatchMessageRequest.builder()
            .message_id(message_id)
            .request_body(body)
            .build()
        )
        resp = client.im.v1.message.patch(req)
        if not resp.success():
            log.warning(f"feishu patch_card err: code={resp.code} msg={resp.msg} mid={message_id}")
            return False
        return True

    def _add_reaction_sync(self, message_id: str, emoji_type: str = "OnIt") -> None:
        """同步给消息打 emoji reaction (在 asyncio.to_thread 里调)。
        emoji_type 参考飞书文档: 'OnIt' = 👀, 'DONE' = ✅ 等。
        """
        lark = self._lark
        import lark_oapi.api.im.v1 as im_v1

        client = lark.Client.builder().app_id(self.app_id).app_secret(self.app_secret).build()
        body = (
            im_v1.CreateMessageReactionRequestBody.builder()
            .reaction_type(
                im_v1.Emoji.builder().emoji_type(emoji_type).build()
            )
            .build()
        )
        req = (
            im_v1.CreateMessageReactionRequest.builder()
            .message_id(message_id)
            .request_body(body)
            .build()
        )
        resp = client.im.v1.message_reaction.create(req)
        if not resp.success():
            log.debug(
                f"feishu add_reaction err: code={resp.code} msg={resp.msg} mid={message_id}"
            )

    # ────────── Frontend 接口实现 ──────────

    async def send_html(self, chat_id: int | str, thread_id: int | None, html_text: str) -> Any:
        """HTML → 飞书 Markdown → interactive card。返回带 .message_id 的对象供 edit 用。"""
        md = _html_to_feishu_md(html_text)
        message_id = await asyncio.to_thread(self._send_card_sync, str(chat_id), md)
        if message_id is None:
            return None
        return _make_fake_msg(message_id)

    async def edit_html(self, chat_id: int | str, message_id: str, html_text: str) -> None:
        """PATCH 更新已发 card 内容 (工具调用聚合器使用)"""
        md = _html_to_feishu_md(html_text)
        await asyncio.to_thread(self._patch_card_sync, message_id, md)

    async def send_pre(self, chat_id: int | str, thread_id: int | None, raw_text: str) -> None:
        """raw_text 用代码块包裹后发 card"""
        if not raw_text.strip():
            return
        md = "```\n" + raw_text + "\n```"
        await asyncio.to_thread(self._send_card_sync, str(chat_id), md)

    async def send_chat_action(self, chat_id: int | str, thread_id: int | None, action: str) -> None:
        """飞书无 typing 状态 API → no-op"""
        return

    # ────────── auto-provision (/init 自动开通会话) ──────────

    def _fetch_chat_name_sync(self, chat_id: str) -> str | None:
        """同步取群名 (在 asyncio.to_thread 里调)。失败返回 None。

        两步: ① app_id/app_secret 换 tenant_access_token
              ② 带 Bearer 调 GET /im/v1/chats/{chat_id} 取 data.name
        优先用 requests, 没装则降级 urllib (纯 stdlib)。
        """
        try:
            import requests  # type: ignore
        except ImportError:
            requests = None

        token_url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
        token_body = {"app_id": self.app_id, "app_secret": self.app_secret}

        if requests is not None:
            r = requests.post(token_url, json=token_body, timeout=10)
            tok = (r.json() or {}).get("tenant_access_token")
            if not tok:
                log.warning(f"feishu tenant_token err: {r.text[:200]}")
                return None
            chat_url = f"https://open.feishu.cn/open-apis/im/v1/chats/{chat_id}"
            cr = requests.get(chat_url, headers={"Authorization": f"Bearer {tok}"}, timeout=10)
            data = (cr.json() or {}).get("data") or {}
            return data.get("name") or None
        else:
            import urllib.request
            req = urllib.request.Request(
                token_url,
                data=json.dumps(token_body).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                tok = (json.loads(resp.read().decode("utf-8")) or {}).get("tenant_access_token")
            if not tok:
                log.warning("feishu tenant_token err (urllib)")
                return None
            chat_url = f"https://open.feishu.cn/open-apis/im/v1/chats/{chat_id}"
            creq = urllib.request.Request(
                chat_url, headers={"Authorization": f"Bearer {tok}"}, method="GET"
            )
            with urllib.request.urlopen(creq, timeout=10) as resp:
                data = (json.loads(resp.read().decode("utf-8")) or {}).get("data") or {}
            return data.get("name") or None

    def _list_projects(self) -> str:
        """列 project_base 下的直接子目录, 返回飞书 HTML 文本 (供 /projects 用)"""
        base = self.project_base
        try:
            dirs = sorted(
                d for d in os.listdir(base)
                if os.path.isdir(os.path.join(base, d))
            )
        except OSError:
            dirs = []
        body = "\n".join(f"• {html_mod.escape(d)}" for d in dirs) if dirs else "（空）"
        return (
            f"📂 <b>项目目录</b> (base: <code>{html_mod.escape(base)}</code>)\n"
            f"{body}\n\n"
            "用法: <code>/init &lt;目录名&gt;</code> 绑定; <code>/init</code> 自动用群名新建"
        )

    async def _auto_provision(self, chat_id: str, chat_type: str, target_dir: str | None = None) -> None:
        """飞书 /init: 取群名 → 调公共 provision_chat → 回确认 / 失败卡片。

        provision 逻辑 (建目录 / 信任 / tmux / binding / tailer / yaml / 起 claude) 已抽到
        tmuxbot.provision.provision_chat, 这里只负责飞书特有的取群名 + 回卡片。
        """
        from tmuxbot.provision import AsciiDirRequired, provision_chat

        # 取群名 (失败 / p2p → 降级名, 交给 provision_chat 的 _safe_name 兜底)
        raw_name = ""
        try:
            if chat_type != "p2p":
                raw_name = await asyncio.to_thread(self._fetch_chat_name_sync, chat_id) or ""
        except Exception as e:
            log.warning(f"auto-provision: 取群名失败 (用降级名): {e}")
            raw_name = ""
        if not raw_name and chat_type == "p2p":
            raw_name = f"feishu-dm-{chat_id[3:11]}"

        try:
            b = await provision_chat(
                self, self.state,
                chat_id=chat_id,
                thread_id=None,
                display_name=raw_name,
                offsets_file=self.offsets_file,
                bindings_file=self.bindings_file,
                bot_token_env=self.bot_token_env,
                project_base=self.project_base,
                channel="feishu",
                target_dir=target_dir,
            )
        except AsciiDirRequired:
            await self.send_html(
                chat_id, None,
                "⚠️ <b>群名含中文,项目目录需英文</b>\n"
                "请用 <code>/init &lt;英文目录名&gt;</code> 指定 (tmux 仍用群名)\n"
                "或 /projects 看现有目录",
            )
            return

        if b is None:
            # 已绑定 → 静默 (provision_chat 已 log); 真失败 → 回卡片
            if self.find_binding(chat_id) is None:
                await self.send_html(
                    chat_id, None,
                    "❌ <b>开通会话失败</b>\n请检查日志或手动配置 bindings.yaml",
                )
            return

        await self.send_html(
            chat_id, None,
            f"✅ <b>已开通会话</b>\n群: {b.name}\n"
            f"目录: <code>{b.cwd}</code>\n现在可以直接对话了",
        )

    # ────────── 消息收发 handler ──────────

    def _on_message(self, data) -> None:
        """lark worker 线程回调: P2ImMessageReceiveV1 → 跳回主 loop 处理"""
        if self._main_loop is None:
            return
        asyncio.run_coroutine_threadsafe(self._handle_message(data), self._main_loop)

    def _on_chat_removed(self, data) -> None:
        """lark worker 线程回调: 群解散 / bot 被移出群 → 跳回主 loop 拆除会话。

        群解散 (p2_im_chat_disbanded_v1) 和 bot 被移除 (p2_im_chat_member_bot_deleted_v1)
        共用此回调 — 两者 event 都带 chat_id, 处理一致 (deprovision 该 binding)。
        """
        if self._main_loop is None:
            return
        try:
            chat_id = data.event.chat_id
        except Exception:
            log.debug("feishu chat_removed event 无 chat_id, 忽略")
            return
        asyncio.run_coroutine_threadsafe(
            self._handle_chat_removed(chat_id), self._main_loop
        )

    async def _handle_chat_removed(self, chat_id: str) -> None:
        """主 loop 里拆除 chat_id 对应的 binding (若有)"""
        from tmuxbot.provision import deprovision_chat
        try:
            b = self.find_binding(chat_id)
            if b is None:
                log.debug(f"feishu chat_removed: chat_id={chat_id} 无 binding, 忽略")
                return
            log.info(f"feishu chat_removed: 拆除会话 chat_id={chat_id} binding={b.name}")
            await deprovision_chat(self, self.state, b, bindings_file=self.bindings_file)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("feishu _handle_chat_removed err")

    async def _handle_message(self, data) -> None:
        """主 loop 里处理收到的飞书消息"""
        try:
            event = data.event
            msg = event.message
            sender = event.sender

            chat_id: str = msg.chat_id          # oc_xxx
            chat_type: str = msg.chat_type      # "group" / "p2p"
            msg_type: str = msg.message_type    # "text" / "image" / ...
            open_id: str = sender.sender_id.open_id

            # 诊断: 收到的每条消息 (open_id 按 app 区分, 新接入时据此配白名单; debug 级不刷屏)
            log.debug(
                f"feishu 收到消息: chat_id={chat_id} chat_type={chat_type} "
                f"open_id={open_id} msg_type={msg_type}"
            )

            # ── ACL 双重门禁 ──
            # 非 Boss 白名单 → 静默
            if not open_id or open_id not in self.boss_open_ids:
                return
            # Boss 发来但 source 未配置 binding:
            #   - text == /projects → 列 base 下现有目录 (未绑定群也能用)
            #   - text 以 /init 开头 → 自动开通会话 (建目录 + tmux + binding + 起 claude)
            #     /init <目录名> → 用指定目录; /init → 用群名新建
            #   - 否则打印 chat_id 提示 (便于加新 binding) 后静默
            b = self.find_binding(chat_id)
            # ── /deinit 手动拆除该 source 的 binding (Boss; 已绑定群) ──
            # 放 ACL 白名单后、/init 检测附近, 在"未绑定静默"分支之前判断:
            # 有 binding → deprovision (复用 provision.deprovision_chat, 不重写);
            # 无 binding → 回提示 (这里是回提示而非静默, 放 ACL 后即可)。
            _text_now = ""
            if msg_type == "text":
                try:
                    _cn = json.loads(msg.content)
                    _text_now = (_cn.get("text", "") or "").strip()
                except (json.JSONDecodeError, AttributeError):
                    _text_now = str(msg.content or "").strip()
                _text_now = re.sub(r"@_user_\d+\s*", "", _text_now).strip()
            if _text_now == "/deinit":
                from tmuxbot.provision import deprovision_chat
                if b is None:
                    await self.send_html(chat_id, None, "本群/话题未绑定,无需拆除")
                    return
                _name = b.name
                await deprovision_chat(self, self.state, b, bindings_file=self.bindings_file)
                await self.send_html(
                    chat_id, None,
                    f"✅ 已拆除会话「{_name}」\n"
                    "tmux 已关 · binding 注销\n"
                    "项目目录和历史 jsonl 保留(可重新 /init 接回)",
                )
                return

            if b is None:
                _text_for_init = ""
                if msg_type == "text":
                    try:
                        _co = json.loads(msg.content)
                        _text_for_init = (_co.get("text", "") or "").strip()
                    except (json.JSONDecodeError, AttributeError):
                        _text_for_init = str(msg.content or "").strip()
                    _text_for_init = re.sub(r"@_user_\d+\s*", "", _text_for_init).strip()
                if _text_for_init == "/projects":
                    await self.send_html(chat_id, None, self._list_projects())
                    return
                if _text_for_init.startswith("/init"):
                    _parts = _text_for_init.split(maxsplit=1)
                    _arg = _parts[1].strip() if len(_parts) > 1 else None
                    await self._auto_provision(chat_id, chat_type, target_dir=_arg)
                    return
                log.info(
                    f"feishu 未配置 source: chat_id={chat_id} chat_type={chat_type} "
                    f"(来自 Boss open_id={open_id[:10]}…, /init 可自动开通, 或在 bindings.yaml 手配)"
                )
                return

            # ── 群消息: group_only_when_mentioned 过滤 ──
            if chat_type == "group" and self.group_only_when_mentioned:
                # mentions 里找有没有 @bot (open_id = bot 自己)
                mentions = getattr(msg, "mentions", None) or []
                bot_mentioned = any(
                    getattr(getattr(m, "id", None), "open_id", None) == self.app_id
                    for m in mentions
                )
                if not bot_mentioned:
                    return

            # ── 只处理 text 类型 ──
            if msg_type != "text":
                log.debug(f"feishu: ignore non-text msg_type={msg_type}")
                return

            # ── 解析文本内容 ──
            try:
                content_obj = json.loads(msg.content)
                text: str = content_obj.get("text", "")
            except (json.JSONDecodeError, AttributeError):
                text = str(msg.content or "")

            # 清掉群消息里的 @_user_N 占位符
            text = re.sub(r"@_user_\d+\s*", "", text).strip()

            if not text:
                return

            # ── /projects: 列 base 下目录 (已绑定群也能用, 纯信息不进 dispatch) ──
            if text == "/projects":
                await self.send_html(chat_id, None, self._list_projects())
                return

            # ── 👀 已读 reaction (ACL 通过后打, 失败不影响主流程) ──
            self.state.last_active[b.name] = time.time()
            try:
                await asyncio.to_thread(self._add_reaction_sync, msg.message_id, "OnIt")
            except Exception as e:
                log.debug(f"feishu reaction err: {e}")

            # ── 命令分发 (共享层: stop / capture 命令 / 普通文本) ──
            from tmuxbot.dispatch import dispatch_incoming_text
            await dispatch_incoming_text(
                self, self.backend, b, self.state,
                chat_id, None, text,
            )

        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("feishu _handle_message err")

    # ────────── 启动 / 停止 ──────────

    async def start_polling(self) -> None:
        """建 WebSocket 长连接, 阻塞直到 stop() 被调用"""
        lark = self._lark
        import lark_oapi.ws.client as _wsc

        self._main_loop = asyncio.get_running_loop()

        builder = (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(self._on_message)
        )
        # 群解散 + bot 被移出群 → 自动拆除会话。不同 lark-oapi 版本方法名可能缺,
        # 用 getattr 防御性注册: 缺哪个只 warning, 不影响消息收发主链路。
        for _evt_method in (
            "register_p2_im_chat_disbanded_v1",          # 群解散
            "register_p2_im_chat_member_bot_deleted_v1",  # bot 被移出群
        ):
            _reg = getattr(builder, _evt_method, None)
            if _reg is not None:
                _reg(self._on_chat_removed)
            else:
                log.warning(f"feishu: lark-oapi 缺 {_evt_method}, 跳过该解散事件注册")
        handler = builder.build()
        self._ws_client = lark.ws.Client(
            self.app_id,
            self.app_secret,
            event_handler=handler,
            log_level=lark.LogLevel.WARNING,
        )

        log.info(f"feishu ws starting · app_id={self.app_id[:8]}… · {len(self.bindings)} bindings")

        stop_event = asyncio.Event()
        self._stop_event = stop_event

        def _run():
            # ★ SDK 必须在同一 worker thread 里建新 event loop, 并覆盖 SDK 模块级 loop
            # 否则 "loop already running" 报错
            import asyncio as _asyncio
            nl = _asyncio.new_event_loop()
            _asyncio.set_event_loop(nl)
            _wsc.loop = nl
            self._ws_client.start()

        # 在 to_thread 里阻塞跑 ws client; start_polling 本身阻塞在 stop_event
        ws_task = asyncio.get_running_loop().run_in_executor(None, _run)
        try:
            await stop_event.wait()
        finally:
            ws_task.cancel()
            try:
                await ws_task
            except Exception:
                pass

    async def stop(self) -> None:
        """停止 WebSocket 长连接"""
        log.info("feishu ws stopping")
        if hasattr(self, "_stop_event"):
            self._stop_event.set()
        if self._ws_client is not None:
            try:
                self._ws_client.stop()
            except Exception as e:
                log.debug(f"feishu ws stop err: {e}")
