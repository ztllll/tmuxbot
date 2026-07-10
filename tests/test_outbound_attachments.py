import asyncio
from pathlib import Path
from types import SimpleNamespace

from tmuxbot.jsonl import on_tmux_event
from tmuxbot.attachments import is_image_file
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

    async def send_assistant_reply(self, binding, envelope):
        if envelope.body.strip():
            await self.send_html(binding.chat_id, binding.thread_id, envelope.body)
        for path in envelope.attachments:
            if is_image_file(path):
                await self.send_image(binding.chat_id, binding.thread_id, path)
            else:
                await self.send_file(binding.chat_id, binding.thread_id, path)


class EnhancedFakeFrontend(FakeFrontend):
    async def send_assistant_reply(self, binding, envelope):
        self.sent.append(
            (
                "assistant_reply",
                binding.chat_id,
                binding.thread_id,
                envelope.body,
                tuple(Path(path) for path in envelope.attachments),
            )
        )


class FakeBackend:
    def read_tasks(self, binding):
        return []

    def parse_terminal_status(self, pane):
        return None


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


def test_assistant_text_uses_enhanced_reply_sender_when_available(tmp_path):
    async def run():
        image = tmp_path / "result.jpg"
        image.write_bytes(b"jpg")

        frontend = EnhancedFakeFrontend()
        state = SimpleNamespace(setup_mode=False)
        b = binding(tmp_path)

        await on_tmux_event(
            b,
            "assistant_text",
            f"结果如下\n@{image}",
            frontend,
            state,
            FakeBackend(),
        )

        assert frontend.sent == [
            ("assistant_reply", 123, None, "结果如下", (image,)),
        ]

    asyncio.run(run())


def test_assistant_text_promotes_relative_markdown_link_from_binding_cwd(tmp_path):
    async def run():
        report = tmp_path / "reports" / "result.pdf"
        report.parent.mkdir()
        report.write_bytes(b"pdf")

        frontend = EnhancedFakeFrontend()
        state = SimpleNamespace(setup_mode=False)
        b = binding(tmp_path)

        await on_tmux_event(
            b,
            "assistant_text",
            "结果文件：[下载](<./reports/result.pdf>)",
            frontend,
            state,
            FakeBackend(),
        )

        assert frontend.sent == [
            ("assistant_reply", 123, None, "结果文件：下载", (report,)),
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


def test_live_text_sends_early_and_final_duplicate_is_skipped(tmp_path):
    async def run():
        frontend = FakeFrontend()
        state = SimpleNamespace(setup_mode=False)
        b = binding(tmp_path)
        backend = FakeBackend()

        await on_tmux_event(
            b,
            "assistant_live_text",
            "我先检查配置，再给结论。",
            frontend,
            state,
            backend,
        )
        await on_tmux_event(
            b,
            "assistant_text",
            "我先检查配置，再给结论。",
            frontend,
            state,
            backend,
        )

        assert frontend.sent == [
            ("html", 123, None, "我先检查配置，再给结论。"),
        ]

    asyncio.run(run())


def test_text_delta_stream_edits_one_reply_and_finalizes(tmp_path):
    async def run():
        frontend = FakeFrontend()
        state = SimpleNamespace(setup_mode=False)
        b = binding(tmp_path)
        backend = FakeBackend()

        await on_tmux_event(b, "assistant_text_delta", "正在", frontend, state, backend)
        await on_tmux_event(b, "assistant_text_delta", "检查", frontend, state, backend)
        await on_tmux_event(b, "assistant_text", "正在检查配置。", frontend, state, backend)

        assert frontend.sent == [
            ("html", 123, None, "正在"),
            ("edit", 123, 101, "正在检查"),
            ("edit", 123, 101, "正在检查配置。"),
        ]

    asyncio.run(run())
