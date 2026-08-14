import asyncio
from pathlib import Path
from types import SimpleNamespace

from tmuxbot import jsonl
from tmuxbot.core.events import ProviderRuntimeMetadata, TerminalState, TerminalStatus
from tmuxbot.jsonl import _capture_terminal_status, on_tmux_event
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


class FinalizingFrontend(FakeFrontend):
    async def finalize_status_html(
        self, chat_id, message_id, html_text, *, display_state="completed"
    ):
        self.sent.append(("finalize", chat_id, message_id, html_text, display_state))


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


class StreamingFooterFrontend(FakeFrontend):
    async def send_reply_stream_start(self, binding, html_text):
        self.sent.append(("stream_start", binding.chat_id, html_text))
        self.next_message_id += 1
        return SimpleNamespace(message_id=self.next_message_id)

    async def edit_reply_stream(self, binding, message_id, html_text, *, final=False, footer=None):
        self.sent.append(("stream_edit", binding.chat_id, message_id, html_text, final, footer))


class FakeBackend:
    name = "claude_code"

    def read_tasks(self, binding):
        return []

    def parse_terminal_status(self, pane):
        return None


class OmpTodoBackend(FakeBackend):
    name = "omp"

    def __init__(self, tasks):
        self.tasks = tasks

    def read_tasks(self, binding):
        return self.tasks


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


def test_omp_compaction_end_edits_the_existing_status_message(tmp_path):
    async def run():
        frontend = FinalizingFrontend()
        state = SimpleNamespace(
            setup_mode=False,
            compaction_status={"alpha": {"msg_id": 101, "chat_id": 123, "started_at": 1000.0}},
        )
        b = binding(tmp_path)
        backend = OmpTodoBackend(
            [{"phase": "Execution", "content": "Continue", "status": "in_progress"}]
        )

        await on_tmux_event(
            b,
            "provider_lifecycle",
            "✅ <b>OMP 自动压缩已完成</b>",
            frontend,
            state,
            backend,
        )

        assert state.compaction_status == {}
        assert frontend.sent == [
            (
                "finalize",
                123,
                101,
                "✅ <b>OMP 自动压缩已完成</b>\n\n● <b>Todos (0/1)</b>\n"
                "▾ <b>Execution</b> (0/1)\n└─ ◐ <b>Continue</b>",
                "completed",
            )
        ]

    asyncio.run(run())


def test_omp_plan_widget_is_appended_above_todos_in_final_assistant_message(tmp_path):
    async def run():
        class PlanBackend(OmpTodoBackend):
            def render_extension_footer(self, _binding):
                return "📝 <b>计划已就绪</b>\n· 使用 <code>/plan</code> 选择下一步。"

        frontend = FakeFrontend()
        state = SimpleNamespace(setup_mode=False)
        b = binding(tmp_path)
        backend = PlanBackend([{"phase": "Review", "content": "Review", "status": "pending"}])

        await on_tmux_event(b, "assistant_text", "计划如下", frontend, state, backend)

        assert frontend.sent == [
            (
                "html",
                123,
                None,
                "计划如下\n\n"
                "📝 <b>计划已就绪</b>\n· 使用 <code>/plan</code> 选择下一步。\n\n"
                "● <b>Todos (0/1)</b>\n▾ <b>Review</b> (0/1)\n└─ ○ Review",
            )
        ]

    asyncio.run(run())


def test_omp_todo_snapshot_is_appended_to_every_final_assistant_message(tmp_path):
    async def run():
        tasks = [
            {"phase": "Execution", "content": "Audit", "status": "completed"},
            {"phase": "Execution", "content": "Implement", "status": "completed"},
        ]
        frontend = FakeFrontend()
        state = SimpleNamespace(setup_mode=False)
        b = binding(tmp_path)
        backend = OmpTodoBackend(tasks)

        await on_tmux_event(b, "assistant_text", "第一条", frontend, state, backend)
        await on_tmux_event(b, "assistant_text", "第二条", frontend, state, backend)

        panel = (
            "○ <b>Todos (2/2)</b>\n▾ <b>Execution</b> (2/2)\n"
            "├─ ✓ <s>Audit</s>\n└─ ✓ <s>Implement</s>"
        )
        assert frontend.sent == [
            ("html", 123, None, f"第一条\n\n{panel}"),
            ("html", 123, None, f"第二条\n\n{panel}"),
        ]

    asyncio.run(run())


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


def test_status_capture_uses_transcript_model_when_tui_omits_it(tmp_path, monkeypatch):
    class BackendWithModel:
        def parse_terminal_status(self, pane):
            # TUI scrollback may contain a subagent/tool label that looks like a model.
            return TerminalStatus(state=TerminalState.WORKING, model="claude-code-guide")

        def current_runtime_metadata(self, binding):
            return ProviderRuntimeMetadata(
                model="claude-opus-4-8",
                effort="high",
                permission_mode="YOLO",
            )

    monkeypatch.setattr(jsonl, "tmux_capture", lambda target, lines: "working")

    status = asyncio.run(_capture_terminal_status(binding(tmp_path), BackendWithModel()))

    assert status is not None
    assert status.model == "claude-opus-4-8"
    assert status.effort == "high"
    assert status.permission_mode == "YOLO"


def test_status_capture_preserves_tui_usage_and_fills_missing_metadata(tmp_path, monkeypatch):
    class BackendWithUsage:
        def parse_terminal_status(self, pane):
            return TerminalStatus(
                state=TerminalState.IDLE,
                model="gpt-5.6-luna",
                input_tokens=48_000,
                output_tokens=2_300,
                context_used=26_000,
                context_limit=360_000,
            )

        def current_runtime_metadata(self, binding):
            return ProviderRuntimeMetadata(
                provider="aisupertoken",
                model="gpt-5.6-luna",
                effort="medium",
                input_tokens=49_123,
                output_tokens=2_456,
                cache_read_tokens=98_000,
            )

    monkeypatch.setattr(jsonl, "tmux_capture", lambda target, lines: "footer")

    status = asyncio.run(_capture_terminal_status(binding(tmp_path), BackendWithUsage()))

    assert status is not None
    assert status.provider == "aisupertoken"
    assert status.effort == "medium"
    assert status.input_tokens == 48_000
    assert status.output_tokens == 2_300
    assert status.cache_read_tokens == 98_000
    assert status.context_used == 26_000
    assert status.context_limit == 360_000


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


def test_omp_todo_snapshot_is_fixed_to_working_status_messages(tmp_path):
    async def run():
        tasks = [
            {"phase": "Execution", "content": "Audit", "status": "completed"},
            {"phase": "Execution", "content": "Implement", "status": "in_progress"},
        ]
        frontend = FakeFrontend()
        state = SimpleNamespace(setup_mode=False, tool_aggregator={}, fire=close_coro)
        b = binding(tmp_path)

        await on_tmux_event(
            b,
            "assistant_tools",
            "📋 任务 update",
            frontend,
            state,
            OmpTodoBackend(tasks),
        )

        assert frontend.sent == [
            (
                "html",
                123,
                None,
                "💭 <b>工作中…</b>\n📋 任务 update\n\n"
                "● <b>Todos (1/2)</b>\n"
                "▾ <b>Execution</b> (1/2)\n"
                "├─ ✓ <s>Audit</s>\n"
                "└─ ◐ <b>Implement</b>",
            )
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


def test_final_assistant_text_immediately_completes_the_tool_status_card(tmp_path):
    async def run():
        frontend = FinalizingFrontend()
        state = SimpleNamespace(
            setup_mode=False,
            tool_aggregator={
                "alpha": {
                    "msg_id": "om-working",
                    "chat_id": 123,
                    "content": ["💭 <b>工作中…</b>", "工具执行"],
                    "last_ts": 0,
                }
            },
        )
        b = binding(tmp_path)

        await on_tmux_event(b, "assistant_text", "任务完成", frontend, state, FakeBackend())

        assert state.tool_aggregator == {}
        assert frontend.sent[0] == (
            "finalize",
            123,
            "om-working",
            "💭 <b>工作中…</b>\n工具执行\n\n<i>✓ 完成</i>",
            "completed",
        )

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


def test_omp_live_text_and_final_duplicate_share_the_same_todo_panel(tmp_path):
    async def run():
        tasks = [{"phase": "Execution", "content": "Done", "status": "completed"}]
        frontend = FakeFrontend()
        state = SimpleNamespace(setup_mode=False)
        b = binding(tmp_path)
        backend = OmpTodoBackend(tasks)

        await on_tmux_event(b, "assistant_live_text", "进度", frontend, state, backend)
        await on_tmux_event(b, "assistant_text", "进度", frontend, state, backend)

        assert frontend.sent == [
            (
                "html",
                123,
                None,
                "进度\n\n○ <b>Todos (1/1)</b>\n▾ <b>Execution</b> (1/1)\n└─ ✓ <s>Done</s>",
            )
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


def test_text_delta_stream_finalizes_with_provider_footer(tmp_path, monkeypatch):
    class BackendWithMetadata(FakeBackend):
        def current_runtime_metadata(self, binding):
            return ProviderRuntimeMetadata(
                model="gpt-5.6-terra",
                effort="medium",
                permission_mode="YOLO",
            )

    monkeypatch.setattr(jsonl, "tmux_capture", lambda target, lines: "working")

    async def run():
        frontend = StreamingFooterFrontend()
        state = SimpleNamespace(setup_mode=False)
        b = binding(tmp_path)

        await on_tmux_event(
            b, "assistant_text_delta", "正在", frontend, state, BackendWithMetadata()
        )
        await on_tmux_event(
            b, "assistant_text", "检查完成。", frontend, state, BackendWithMetadata()
        )

        final = frontend.sent[-1]
        assert final[:5] == ("stream_edit", 123, 101, "检查完成。", True)
        assert final[5] == TerminalStatus(
            state=TerminalState.IDLE,
            model="gpt-5.6-terra",
            effort="medium",
            permission_mode="YOLO",
        )

    asyncio.run(run())
