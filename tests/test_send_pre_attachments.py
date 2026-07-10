import asyncio
from pathlib import Path
from types import SimpleNamespace

from tmuxbot.frontends.feishu import FeishuFrontend
from tmuxbot.frontends.telegram import TelegramFrontend


def test_telegram_send_pre_sends_screen_paths_as_real_attachments(tmp_path):
    async def run():
        image = tmp_path / "screen.jpg"
        image.write_bytes(b"jpg")
        sent = []

        frontend = object.__new__(TelegramFrontend)
        frontend.bindings = [SimpleNamespace(chat_id=123, thread_id=456, cwd=tmp_path)]
        frontend.bot = SimpleNamespace(
            send_message=lambda chat_id, text, message_thread_id=None: sent.append(
                ("html", chat_id, message_thread_id, text)
            )
        )

        async def tg_call(fn, max_retries=4):
            return fn()

        async def send_image(chat_id, thread_id, path, caption=None):
            sent.append(("image", chat_id, thread_id, Path(path), caption))

        async def send_file(chat_id, thread_id, path, caption=None):
            sent.append(("file", chat_id, thread_id, Path(path), caption))

        frontend._tg_call = tg_call
        frontend.send_image = send_image
        frontend.send_file = send_file

        await frontend.send_pre(123, 456, "screen\n│ @./screen.jpg")

        assert sent == [
            ("html", 123, 456, "<pre>screen</pre>"),
            ("image", 123, 456, image, None),
        ]

    asyncio.run(run())


def test_feishu_send_pre_sends_screen_paths_as_real_attachments(tmp_path):
    async def run():
        image = tmp_path / "screen.jpg"
        image.write_bytes(b"jpg")
        sent = []

        frontend = object.__new__(FeishuFrontend)
        frontend.bindings = [SimpleNamespace(chat_id="oc_x", thread_id=None, cwd=tmp_path)]
        frontend._send_card_sync = lambda chat_id, md: sent.append(("card", chat_id, md))

        async def send_image(chat_id, thread_id, path, caption=None):
            sent.append(("image", chat_id, thread_id, Path(path), caption))

        async def send_file(chat_id, thread_id, path, caption=None):
            sent.append(("file", chat_id, thread_id, Path(path), caption))

        frontend.send_image = send_image
        frontend.send_file = send_file

        await frontend.send_pre("oc_x", None, "screen\n│ @./screen.jpg")

        assert sent == [
            ("card", "oc_x", "```\nscreen\n```"),
            ("image", "oc_x", None, image, None),
        ]

    asyncio.run(run())
