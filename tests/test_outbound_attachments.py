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


class FailingEditFrontend(FinalizingFrontend):
    async def edit_html(self, chat_id, message_id, html_text):
        raise RuntimeError("message can no longer be edited")

    async def finalize_status_html(
        self, chat_id, message_id, html_text, *, display_state="completed"
    ):
        raise RuntimeError("message can no longer be finalized")


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

    async def edit_reply_stream(
        self, binding, message_id, html_text, *, final=False, footer=None
    ):
        self.sent.append(
            ("stream_edit", binding.chat_id, message_id, html_text, final, footer)
        )


class FakeBackend:
    name = "claude_code"

    def read_tasks(self, binding):
        return []

    def parse_terminal_status(self, pane):
        return None


class PiTodoBackend(FakeBackend):
    name = "pi"

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


def test_pi_compaction_end_edits_the_existing_status_message(tmp_path):
    async def run():
        frontend = FinalizingFrontend()
        state = SimpleNamespace(
            setup_mode=False,
            compaction_status={
                "alpha": {"msg_id": 101, "chat_id": 123, "started_at": 1000.0}
            },
        )
        b = binding(tmp_path)
        backend = PiTodoBackend(
            [{"id": 1, "subject": "Continue", "status": "in_progress"}]
        )

        await on_tmux_event(
            b,
            "provider_lifecycle",
            "✅ <b>Pi 自动压缩已完成</b>",
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
                "✅ <b>Pi 自动压缩已完成</b>\n\n"
                "● <b>Todos (0/1)</b>\n└─ ◐ <b>Continue</b>",
                "completed",
            )
        ]

    asyncio.run(run())


def test_pi_plan_widget_is_appended_above_todos_in_final_assistant_message(tmp_path):
    async def run():
        class PlanBackend(PiTodoBackend):
            def render_extension_footer(self, _binding):
                return "📝 <b>计划已就绪</b>\n· 使用 <code>/plan</code> 选择下一步。"

        frontend = FakeFrontend()
        state = SimpleNamespace(setup_mode=False)
        b = binding(tmp_path)
        backend = PlanBackend([{"id": 1, "subject": "Review", "status": "pending"}])

        await on_tmux_event(b, "assistant_text", "计划如下", frontend, state, backend)

        assert frontend.sent == [
            (
                "html",
                123,
                None,
                "计划如下\n\n"
                "📝 <b>计划已就绪</b>\n· 使用 <code>/plan</code> 选择下一步。\n\n"
                "● <b>Todos (0/1)</b>\n└─ ○ Review",
            )
        ]

    asyncio.run(run())


def test_pi_todo_snapshot_is_appended_to_every_final_assistant_message(tmp_path):
    async def run():
        tasks = [
            {"id": 1, "subject": "Audit", "status": "completed"},
            {"id": 2, "subject": "Implement", "status": "completed", "blockedBy": [1]},
        ]
        frontend = FakeFrontend()
        state = SimpleNamespace(setup_mode=False)
        b = binding(tmp_path)
        backend = PiTodoBackend(tasks)

        await on_tmux_event(b, "assistant_text", "第一条", frontend, state, backend)
        await on_tmux_event(b, "assistant_text", "第二条", frontend, state, backend)

        panel = (
            "○ <b>Todos (2/2)</b>\n"
            "├─ ✓ #1 <s>Audit</s>\n"
            "└─ ✓ #2 <s>Implement</s> ⛓ #1"
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


def test_status_capture_falls_back_to_binding_cwd_when_tui_omits_path(
    tmp_path, monkeypatch
):
    class BackendWithoutCwd:
        def parse_terminal_status(self, pane):
            return TerminalStatus(state=TerminalState.IDLE, model="gpt-5.6-sol")

        def current_runtime_metadata(self, binding):
            return ProviderRuntimeMetadata(model="gpt-5.6-sol")

    monkeypatch.setattr(jsonl, "tmux_capture", lambda target, lines: "footer without cwd")

    status = asyncio.run(_capture_terminal_status(binding(tmp_path), BackendWithoutCwd()))

    assert status is not None
    assert status.cwd == str(tmp_path)


def test_status_capture_creates_path_only_footer_when_tui_and_transcript_are_pending(
    tmp_path, monkeypatch
):
    class PendingBackend:
        def parse_terminal_status(self, pane):
            return None

        def current_runtime_metadata(self, binding):
            return ProviderRuntimeMetadata()

    monkeypatch.setattr(jsonl, "tmux_capture", lambda target, lines: "starting")

    status = asyncio.run(_capture_terminal_status(binding(tmp_path), PendingBackend()))

    assert status is not None
    assert status.cwd == str(tmp_path)


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


def test_pi_todo_snapshot_is_fixed_to_working_status_messages(tmp_path):
    async def run():
        tasks = [
            {"id": 1, "subject": "Audit", "status": "completed"},
            {"id": 2, "subject": "Implement", "status": "in_progress", "blockedBy": [1]},
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
            PiTodoBackend(tasks),
        )

        assert frontend.sent == [
            (
                "html",
                123,
                None,
                "💭 <b>工作进度</b>\n"
                "· 当前：📋 任务 update\n"
                "· 统计：工具 1",
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
            (
                "html", 123, None,
                "💭 <b>工作进度</b>\n· 当前：工具输出\n· 统计：工具 1",
            ),
            ("image", 123, None, image, None),
        ]

    asyncio.run(run())


def test_quick_result_finishes_before_delay_without_creating_progress_card(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("TMUXBOT_IM_PROGRESS_DELAY", "4")

    async def run():
        frontend = FinalizingFrontend()
        state = SimpleNamespace(setup_mode=False)
        b = binding(tmp_path)
        backend = FakeBackend()

        await on_tmux_event(b, "assistant_tools", "读取配置", frontend, state, backend)
        await on_tmux_event(b, "assistant_text", "配置正常", frontend, state, backend)

        assert frontend.sent == [("html", 123, None, "配置正常")]

    asyncio.run(run())


def test_result_only_mode_suppresses_progress_but_not_attention_or_result(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("TMUXBOT_IM_PRESENTATION", "result_only")

    async def run():
        frontend = FinalizingFrontend()
        state = SimpleNamespace(setup_mode=False)
        b = binding(tmp_path)
        backend = FakeBackend()

        await on_tmux_event(b, "assistant_tools", "读取配置", frontend, state, backend)
        await on_tmux_event(
            b, "interaction_request", "请选择部署环境", frontend, state, backend
        )
        await on_tmux_event(b, "assistant_text", "已按选择处理", frontend, state, backend)

        assert frontend.sent == [
            ("html", 123, None, "🟠 <b>需要老板处理</b>\n请选择部署环境"),
            ("html", 123, None, "已按选择处理"),
        ]

    asyncio.run(run())


def test_interaction_request_finalizes_progress_and_sends_attention(tmp_path):
    async def run():
        frontend = FinalizingFrontend()
        state = SimpleNamespace(setup_mode=False)
        b = binding(tmp_path)
        backend = FakeBackend()

        await on_tmux_event(b, "assistant_tools", "读取配置", frontend, state, backend)
        await on_tmux_event(
            b, "interaction_request", "请选择部署环境", frontend, state, backend
        )

        assert [item[0] for item in frontend.sent] == ["html", "finalize", "html"]
        assert frontend.sent[1][4] == "waiting"
        assert frontend.sent[2] == (
            "html", 123, None,
            "🟠 <b>需要老板处理</b>\n请选择部署环境",
        )

    asyncio.run(run())


def test_progress_edit_failure_recreates_one_card_and_final_result_still_delivers(tmp_path):
    async def run():
        frontend = FailingEditFrontend()
        state = SimpleNamespace(setup_mode=False)
        b = binding(tmp_path)
        backend = FakeBackend()

        await on_tmux_event(b, "assistant_tools", "读取配置", frontend, state, backend)
        projection = state.turn_projections[b.name]
        projection.update_interval_seconds = 0
        await on_tmux_event(b, "assistant_tools", "修改配置", frontend, state, backend)
        await on_tmux_event(b, "assistant_text", "处理完成", frontend, state, backend)

        assert [item[0] for item in frontend.sent] == ["html", "html", "html", "html"]
        assert frontend.sent[-1] == ("html", 123, None, "处理完成")
        assert state.progress_messages == {}

    asyncio.run(run())


def test_terminal_provider_error_sends_one_attention_message(tmp_path):
    async def run():
        frontend = FinalizingFrontend()
        state = SimpleNamespace(setup_mode=False)
        b = binding(tmp_path)
        backend = FakeBackend()

        await on_tmux_event(b, "assistant_tools", "调用部署工具", frontend, state, backend)
        await on_tmux_event(b, "provider_error", "授权失败", frontend, state, backend)

        assert [item[0] for item in frontend.sent] == ["html", "finalize", "html"]
        assert frontend.sent[1][4] == "error"
        assert frontend.sent[2] == (
            "html", 123, None,
            "🔴 <b>任务未能继续</b>\n授权失败",
        )

    asyncio.run(run())


def test_tool_and_plan_progress_share_one_card_before_final_result(tmp_path):
    async def run():
        frontend = FinalizingFrontend()
        state = SimpleNamespace(
            setup_mode=False,
            progress_messages={},
            turn_projections={},
            progress_flushes={},
            fire=close_coro,
        )
        b = binding(tmp_path)
        backend = FakeBackend()

        await on_tmux_event(b, "assistant_tools", "读取 app.py", frontend, state, backend)
        await on_tmux_event(
            b,
            "assistant_plan",
            "计划更新\n1. 修改 pending\n2. 测试 in_progress",
            frontend,
            state,
            backend,
        )
        await on_tmux_event(b, "assistant_tools", "修改 app.py", frontend, state, backend)
        await on_tmux_event(b, "assistant_text", "修改完成。", frontend, state, backend)

        assert [item[0] for item in frontend.sent] == ["html", "finalize", "html"]
        assert frontend.sent[0][:3] == ("html", 123, None)
        assert frontend.sent[1][1:3] == (123, 101)
        assert "过程摘要" in frontend.sent[1][3]
        assert "工具 2 · 计划 1" in frontend.sent[1][3]
        assert "读取 app.py；计划更新 0/2 完成，1 项进行中；修改 app.py" in frontend.sent[1][3]
        assert frontend.sent[2] == ("html", 123, None, "修改完成。")

    asyncio.run(run())


def test_final_assistant_text_immediately_completes_the_tool_status_card(tmp_path):
    async def run():
        frontend = FinalizingFrontend()
        state = SimpleNamespace(
            setup_mode=False,
            tool_aggregator={
                "alpha": {
                    "msg_id": "om-working", "chat_id": 123,
                    "content": ["💭 <b>工作中…</b>", "工具执行"], "last_ts": 0,
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
            (
                "html", 123, None,
                "💭 <b>工作进度</b>\n"
                "· 当前：计划更新 0/1 完成，1 项进行中\n"
                "· 统计：计划 1",
            ),
        ]

    asyncio.run(run())


def test_pi_live_text_waits_for_final_and_uses_one_todo_panel(tmp_path):
    async def run():
        tasks = [{"id": 1, "subject": "Done", "status": "completed"}]
        frontend = FakeFrontend()
        state = SimpleNamespace(setup_mode=False)
        b = binding(tmp_path)
        backend = PiTodoBackend(tasks)

        await on_tmux_event(b, "assistant_live_text", "进度", frontend, state, backend)
        assert frontend.sent == []
        await on_tmux_event(b, "assistant_text", "进度", frontend, state, backend)

        assert frontend.sent == [
            (
                "html",
                123,
                None,
                "进度\n\n○ <b>Todos (1/1)</b>\n└─ ✓ <s>Done</s>",
            )
        ]

    asyncio.run(run())


def test_text_delta_is_buffered_until_one_final_result_is_published(tmp_path):
    async def run():
        frontend = EnhancedFakeFrontend()
        state = SimpleNamespace(setup_mode=False)
        b = binding(tmp_path)
        backend = FakeBackend()

        await on_tmux_event(b, "assistant_text_delta", "正在", frontend, state, backend)
        await on_tmux_event(b, "assistant_text_delta", "检查", frontend, state, backend)
        assert frontend.sent == []

        await on_tmux_event(b, "assistant_text", "正在检查配置。", frontend, state, backend)
        await on_tmux_event(b, "assistant_text", "正在检查配置。", frontend, state, backend)

        assert frontend.sent == [
            ("assistant_reply", 123, None, "正在检查配置。", ()),
        ]
        assert state.result_drafts == {}
        assert state.im_delivery_metrics["alpha"]["results_published"] == 1
        assert state.im_delivery_metrics["alpha"]["duplicate_results_suppressed"] == 1
        assert state.im_delivery_metrics["alpha"]["result_body_chars"] == len(
            "正在检查配置。"
        )

    asyncio.run(run())


def test_live_text_is_buffered_until_final_result(tmp_path):
    async def run():
        frontend = EnhancedFakeFrontend()
        state = SimpleNamespace(setup_mode=False)
        b = binding(tmp_path)
        backend = FakeBackend()

        await on_tmux_event(
            b, "assistant_live_text", "我先检查配置。", frontend, state, backend
        )
        assert frontend.sent == []

        await on_tmux_event(
            b, "assistant_text", "检查完成。", frontend, state, backend
        )

        assert frontend.sent == [
            ("assistant_reply", 123, None, "检查完成。", ()),
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
        frontend = EnhancedFakeFrontend()
        state = SimpleNamespace(setup_mode=False)
        b = binding(tmp_path)

        await on_tmux_event(
            b, "assistant_text_delta", "正在", frontend, state, BackendWithMetadata()
        )
        await on_tmux_event(
            b, "assistant_text", "检查完成。", frontend, state, BackendWithMetadata()
        )

        assert frontend.sent == [
            ("assistant_reply", 123, None, "检查完成。", ()),
        ]

    asyncio.run(run())
