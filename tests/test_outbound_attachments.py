import asyncio
from pathlib import Path
from types import SimpleNamespace

from tmuxbot.jsonl import on_tmux_event
from tmuxbot.state import Binding


def close_coro(coro):
    coro.close()


class FakeFrontend:
    def __init__(self) -> None:
        self.bindings = []
        self.sent = []
        self.next_message_id = 100

    async def send_html(self, chat_id, thread_id, html_text):
        self.sent.append(("html", chat_id, thread_id, html_text))
        self.next_message_id += 1
        return SimpleNamespace(message_id=self.next_message_id)

    async def send_image(self, chat_id, thread_id, path, caption=None):
        self.sent.append(("image", chat_id, thread_id, Path(path), caption))

    async def send_file(self, chat_id, thread_id, path, caption=None):
        self.sent.append(("file", chat_id, thread_id, Path(path), caption))

    async def edit_html(self, chat_id, message_id, html_text):
        self.sent.append(("edit", chat_id, message_id, html_text))


class FakeBackend:
    def read_tasks(self, binding):
        return []


def binding(tmp_path):
    return Binding(
        name="alpha",
        chat_id=123,
        thread_id=None,
        tmux_session="alpha-session",
        tmux_window=0,
        tmux_pane=0,
        cwd=tmp_path,
    )


def test_assistant_text_sends_local_paths_as_real_attachments(tmp_path):
    async def run():
        image = tmp_path / "result.jpg"
        image.write_bytes(b"jpg")
        data = tmp_path / "result.csv"
        data.write_text("a,b\n1,2\n")

        frontend = FakeFrontend()
        state = SimpleNamespace(setup_mode=False)
        b = binding(tmp_path)

        await on_tmux_event(
            b,
            "assistant_text",
            f"结果如下\n@{image}\n{data}",
            frontend,
            state,
            FakeBackend(),
        )

        assert frontend.sent == [
            ("html", 123, None, "结果如下"),
            ("image", 123, None, image, None),
            ("file", 123, None, data, None),
        ]

    asyncio.run(run())


def test_assistant_tools_sends_local_paths_as_real_attachments(tmp_path):
    async def run():
        image = tmp_path / "tool-screen.jpg"
        image.write_bytes(b"jpg")

        frontend = FakeFrontend()
        state = SimpleNamespace(setup_mode=False, tool_aggregator={}, fire=close_coro)
        b = binding(tmp_path)

        await on_tmux_event(
            b,
            "assistant_tools",
            f"工具输出\n│ @{image}",
            frontend,
            state,
            FakeBackend(),
        )

        assert frontend.sent == [
            ("html", 123, None, "💭 <b>工作中…</b>\n工具输出"),
            ("image", 123, None, image, None),
        ]

    asyncio.run(run())


def test_assistant_plan_edits_latest_plan_message(tmp_path):
    async def run():
        frontend = FakeFrontend()
        state = SimpleNamespace(setup_mode=False, plan_messages={})
        b = binding(tmp_path)

        await on_tmux_event(
            b,
            "assistant_plan",
            "📋 当前计划\n→ 第一步 <code>in_progress</code>",
            frontend,
            state,
            FakeBackend(),
        )
        await on_tmux_event(
            b,
            "assistant_plan",
            "📋 当前计划\n✓ 第一步 <code>completed</code>\n→ 第二步 <code>in_progress</code>",
            frontend,
            state,
            FakeBackend(),
        )

        assert frontend.sent == [
            ("html", 123, None, "📋 当前计划\n→ 第一步 <code>in_progress</code>"),
            (
                "edit",
                123,
                101,
                "📋 当前计划\n✓ 第一步 <code>completed</code>\n→ 第二步 <code>in_progress</code>",
            ),
        ]

    asyncio.run(run())
