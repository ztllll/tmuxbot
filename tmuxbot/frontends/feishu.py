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
import json
import logging
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
        project_base: str = "/data/project",     # 新项目目录的父目录
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

    def _preseed_trust_sync(self, proj_dir: str) -> None:
        """同步预置 ~/.claude.json 信任 (在 asyncio.to_thread 里调)。失败只 log 不中断。"""
        cfg_path = Path.home() / ".claude.json"
        try:
            cfg = json.loads(cfg_path.read_text()) if cfg_path.exists() else {}
        except Exception as e:
            log.warning(f"auto-provision: 读 ~/.claude.json 失败 (跳过预置信任): {e}")
            return
        projects = cfg.setdefault("projects", {})
        projects[proj_dir] = {
            "hasTrustDialogAccepted": True,
            "hasCompletedProjectOnboarding": True,
            "projectOnboardingSeenCount": 1,
        }
        try:
            cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2))
        except Exception as e:
            log.warning(f"auto-provision: 写 ~/.claude.json 失败 (跳过预置信任): {e}")

    def _persist_binding_sync(self, entry: dict) -> None:
        """同步把新 binding append 到 bindings.yaml (在 asyncio.to_thread 里调)。"""
        import yaml
        if self.bindings_file is None:
            log.warning("auto-provision: bindings_file 未配置, 跳过持久化")
            return
        try:
            raw = yaml.safe_load(self.bindings_file.read_text()) or {}
        except Exception as e:
            log.warning(f"auto-provision: 读 bindings.yaml 失败 (跳过持久化): {e}")
            return
        raw.setdefault("bindings", []).append(entry)
        self.bindings_file.write_text(
            yaml.safe_dump(raw, allow_unicode=True, sort_keys=False)
        )

    async def _auto_provision(self, chat_id: str, chat_type: str) -> None:
        """飞书 /init: 自动建项目目录 + tmux + 注册 binding + 起 claude + 回确认。

        每步失败发卡片告知 Boss 并中止 (不留半成品 binding)。
        """
        from tmuxbot.tmux import tmux_has_session, tmux_new_session

        # 防重复: 已绑定的 chat 直接忽略 (并发 /init 也兜住)
        if self.find_binding(chat_id) is not None:
            log.info(f"auto-provision: chat_id={chat_id} 已绑定, 忽略 /init")
            return

        # 1. 取群名 (失败 / p2p → 降级名)
        try:
            raw_name = None
            if chat_type != "p2p":
                raw_name = await asyncio.to_thread(self._fetch_chat_name_sync, chat_id)
        except Exception as e:
            log.warning(f"auto-provision: 取群名失败 (用降级名): {e}")
            raw_name = None
        if not raw_name:
            raw_name = f"feishu-dm-{chat_id[3:11]}"

        # safe_name: 用于 tmux session 名 + binding name + 目录名。
        # tmux target 用 ':' 和 '.' 分隔, 群名含这俩会破坏 target → 替换成 '-'。
        # 中文 OK (encode_cwd 已能处理)。
        safe_name = re.sub(r"[:.]", "-", raw_name).strip()
        if not safe_name:
            safe_name = f"feishu-dm-{chat_id[3:11]}"

        proj_dir = f"{self.project_base}/{safe_name}"

        try:
            # 2. 防重复 (再查一次 bindings, 双保险)
            if self.find_binding(chat_id) is not None:
                return

            # 3. 建目录
            os.makedirs(proj_dir, exist_ok=True)

            # 4. 预置 claude 信任 (失败只 log)
            await asyncio.to_thread(self._preseed_trust_sync, proj_dir)

            # 5. 建 tmux session
            if not tmux_has_session(safe_name):
                tmux_new_session(safe_name, proj_dir)

            # 6. 注册 binding (内存)
            from tmuxbot.state import Binding
            b = Binding(
                name=safe_name,
                chat_id=chat_id,
                thread_id=None,
                tmux_session=safe_name,
                tmux_window=0,
                tmux_pane=0,
                cwd=Path(proj_dir),
                backend=self.backend.name,
                bot_token_env=self.bot_token_env,
                channel="feishu",
                idle_kill_seconds=1800,
            )
            self.bindings.append(b)
            self.state.bindings.append(b)

            # 7. 起 tailer
            from tmuxbot.jsonl import jsonl_poll_loop
            self.state.fire(
                jsonl_poll_loop(b, self.backend, self, self.state, self.offsets_file)
            )

            # 8. 持久化 bindings.yaml
            entry = {
                "name": safe_name,
                "channel": "feishu",
                "bot_token_env": self.bot_token_env,
                "backend": self.backend.name,
                "chat_id": chat_id,
                "thread_id": None,
                "tmux_session": safe_name,
                "tmux_window": 0,
                "tmux_pane": 0,
                "cwd": proj_dir,
                "idle_kill_seconds": 1800,
            }
            await asyncio.to_thread(self._persist_binding_sync, entry)

            # 9. 起 claude
            await self.backend.ensure_running(b)

        except Exception as e:
            log.exception(f"auto-provision failed for chat_id={chat_id}")
            try:
                await self.send_html(
                    chat_id, None,
                    f"❌ <b>开通会话失败</b>\n{e}\n请检查日志或手动配置 bindings.yaml",
                )
            except Exception:
                pass
            return

        # 10. 回确认
        log.info(f"auto-provision ok: {safe_name} chat_id={chat_id} cwd={proj_dir}")
        await self.send_html(
            chat_id, None,
            f"✅ <b>已开通会话</b>\n群: {safe_name}\n"
            f"目录: <code>{proj_dir}</code>\n现在可以直接对话了",
        )

    # ────────── 消息收发 handler ──────────

    def _on_message(self, data) -> None:
        """lark worker 线程回调: P2ImMessageReceiveV1 → 跳回主 loop 处理"""
        if self._main_loop is None:
            return
        asyncio.run_coroutine_threadsafe(self._handle_message(data), self._main_loop)

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
            #   - text 以 /init 开头 → 自动开通会话 (建目录 + tmux + binding + 起 claude)
            #   - 否则打印 chat_id 提示 (便于加新 binding) 后静默
            b = self.find_binding(chat_id)
            if b is None:
                _text_for_init = ""
                if msg_type == "text":
                    try:
                        _co = json.loads(msg.content)
                        _text_for_init = (_co.get("text", "") or "").strip()
                    except (json.JSONDecodeError, AttributeError):
                        _text_for_init = str(msg.content or "").strip()
                    _text_for_init = re.sub(r"@_user_\d+\s*", "", _text_for_init).strip()
                if _text_for_init.startswith("/init"):
                    await self._auto_provision(chat_id, chat_type)
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

        handler = (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(self._on_message)
            .build()
        )
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
