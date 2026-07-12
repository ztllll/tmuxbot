from types import SimpleNamespace

from tmuxbot.command_adapter import binding_token
from tmuxbot.backends.codex import CodexBackend
from tmuxbot.core.events import TerminalState, TerminalStatus
from tmuxbot.core.replies import ReplyEnvelope
from tmuxbot.frontends.telegram import TG_SPLIT, TelegramFrontend, split_for_tg
from tmuxbot.replies import html_to_plain_text, render_assistant_reply, screen_footer_from_capture
from tmuxbot.state import Binding
from tmuxbot.utils import utf16_len


def binding(tmp_path):
    return Binding(
        name="alpha",
        chat_id=123,
        thread_id=456,
        tmux_session="alpha-session",
        tmux_window=0,
        tmux_pane=0,
        cwd=tmp_path,
        backend="codex",
    )


def test_render_assistant_reply_adds_context_header_and_footer(tmp_path):
    result = render_assistant_reply(
        binding(tmp_path),
        ReplyEnvelope(
            title="回复",
            body="## 结论\n\n```python\nprint(1)\n```",
            footer=TerminalStatus(state=TerminalState.WORKING),
        ),
        full_output_threshold=8000,
        footer_text="• Working (9s • esc to interrupt)",
    )

    assert result.chat_html.startswith(
        f"💬 <b>回复 · {tmp_path.name}</b>\n\n<i>会话 · <code>alpha</code></i>"
    )
    assert "<b>结论</b>" in result.chat_html
    assert '<pre><code class="language-python">print(1)</code></pre>' in result.chat_html
    assert "```python" not in result.chat_html
    assert "• Working (9s • esc to interrupt)" in result.chat_html
    assert "屏幕底部:" not in result.chat_html
    assert "backend=codex" not in result.chat_html
    assert "tmux=codex-tmuxbot:0.0" not in result.chat_html
    assert result.full_text is None


def test_render_assistant_reply_summarizes_long_output_and_keeps_full_text(tmp_path):
    body = "段落\n" * 200

    result = render_assistant_reply(
        binding(tmp_path),
        ReplyEnvelope(title="回复", body=body),
        full_output_threshold=200,
    )

    assert "完整输出已附为文件" in result.chat_html
    assert len(result.chat_html) < len(body)
    assert result.full_text == html_to_plain_text(body)


def test_screen_footer_skips_empty_prompt_and_uses_informative_line():
    raw = """

assistant answer
│
›
"""

    assert screen_footer_from_capture(raw) == "assistant answer"


def test_telegram_assistant_reply_sends_long_output_as_multiple_messages(tmp_path):
    calls = []

    class FakeBot:
        async def send_message(self, chat_id, text, **kwargs):
            calls.append(("message", chat_id, text, kwargs))
            return SimpleNamespace(message_id=901)

        async def send_document(self, chat_id, file, **kwargs):
            calls.append(("document", chat_id, file.filename, kwargs))
            return SimpleNamespace(message_id=902)

    async def run():
        frontend = TelegramFrontend.__new__(TelegramFrontend)
        frontend.bot = FakeBot()

        async def tg_call(fn, max_retries=4):
            return await fn()

        frontend._tg_call = tg_call
        frontend.bot_token_env = "TG_TEST_TOKEN"
        frontend.backend = CodexBackend()

        await frontend.send_assistant_reply(
            binding(tmp_path),
            ReplyEnvelope(
                title="回复",
                body=("长内容\n" * 2000) + "最后一段",
                actions=("screen", "status", "cancel", "interrupt"),
            ),
        )

    import asyncio

    asyncio.run(run())

    assert len(calls) > 1
    assert all(call[0] == "message" for call in calls)
    assert all(call[3]["message_thread_id"] == 456 for call in calls)
    assert all(call[3]["link_preview_options"].is_disabled is True for call in calls)
    assert all(call[3].get("reply_markup") is None for call in calls)
    assert all(utf16_len(call[2]) <= TG_SPLIT for call in calls)
    assert "最后一段" in "".join(call[2] for call in calls)


def test_split_for_tg_splits_single_long_pre_block_without_losing_markup():
    chunks = split_for_tg("<pre>" + ("x" * 9000) + "</pre>")

    assert len(chunks) == 3
    assert all(chunk.startswith("<pre>") and chunk.endswith("</pre>") for chunk in chunks)
    assert all(utf16_len(chunk) <= TG_SPLIT for chunk in chunks)
    assert "".join(html_to_plain_text(chunk) for chunk in chunks) == "x" * 9000


def test_telegram_send_html_splits_long_text_without_document():
    calls = []

    class FakeBot:
        async def send_message(self, chat_id, text, **kwargs):
            calls.append(("message", text, kwargs))
            return SimpleNamespace(message_id=len(calls))

        async def send_document(self, chat_id, file, **kwargs):
            calls.append(("document", file.filename, kwargs))
            return SimpleNamespace(message_id=len(calls))

    async def run():
        frontend = TelegramFrontend.__new__(TelegramFrontend)
        frontend.bot = FakeBot()

        async def tg_call(fn, max_retries=4):
            return await fn()

        frontend._tg_call = tg_call
        await frontend.send_html(123, 456, "正文\n" * 3000)

    import asyncio

    asyncio.run(run())

    assert len(calls) > 1
    assert all(call[0] == "message" for call in calls)
    assert all(call[2]["message_thread_id"] == 456 for call in calls)


def test_telegram_status_cards_use_the_same_project_and_session_header(tmp_path):
    calls = []

    class FakeBot:
        async def send_message(self, chat_id, text, **kwargs):
            calls.append(("send", text, kwargs))
            return SimpleNamespace(message_id=77)

        async def edit_message_text(self, **kwargs):
            calls.append(("edit", kwargs["text"], kwargs))

    async def run():
        frontend = TelegramFrontend.__new__(TelegramFrontend)
        frontend.bot = FakeBot()
        frontend.bindings = [binding(tmp_path)]

        async def tg_call(fn, max_retries=4):
            return await fn()

        frontend._tg_call = tg_call
        await frontend.send_status_html(123, 456, "正在处理", display_state="working")
        await frontend.edit_html(123, 77, "仍在处理")

    import asyncio

    asyncio.run(run())

    expected = f"工作中 · {tmp_path.name}"
    assert expected in calls[0][1]
    assert "会话 · <code>alpha</code>" in calls[0][1]
    assert expected in calls[1][1]


def test_telegram_final_stream_sends_overflow_as_followup_messages(tmp_path):
    calls = []

    class FakeBot:
        async def edit_message_text(self, **kwargs):
            calls.append(("edit", kwargs["text"], kwargs))

        async def send_message(self, chat_id, text, **kwargs):
            calls.append(("message", text, kwargs))
            return SimpleNamespace(message_id=len(calls))

    async def run():
        frontend = TelegramFrontend.__new__(TelegramFrontend)
        frontend.bot = FakeBot()

        async def tg_call(fn, max_retries=4):
            return await fn()

        frontend._tg_call = tg_call
        await frontend.edit_reply_stream(
            binding(tmp_path),
            99,
            "流式正文\n" * 2500,
            final=True,
        )

    import asyncio

    asyncio.run(run())

    assert calls[0][0] == "edit"
    assert any(call[0] == "message" for call in calls[1:])
    assert all(utf16_len(call[1]) <= TG_SPLIT for call in calls)
    assert all(
        call[2].get("message_thread_id") == 456
        for call in calls[1:]
        if call[0] == "message"
    )


def test_telegram_assistant_reply_can_explicitly_enable_link_preview(tmp_path):
    calls = []

    class FakeBot:
        async def send_message(self, chat_id, text, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(message_id=909)

    async def run():
        frontend = TelegramFrontend.__new__(TelegramFrontend)
        frontend.bot = FakeBot()
        frontend.backend = CodexBackend()

        async def tg_call(fn, max_retries=4):
            return await fn()

        frontend._tg_call = tg_call
        await frontend.send_assistant_reply(
            binding(tmp_path),
            ReplyEnvelope(
                title="回复",
                body="https://example.com",
                metadata={"link_preview": True},
            ),
        )

    import asyncio

    asyncio.run(run())

    assert calls[0]["link_preview_options"].is_disabled is False


def test_telegram_assistant_reply_promotes_relative_file_without_exposing_path(tmp_path):
    report = tmp_path / "result.pdf"
    report.write_bytes(b"pdf")
    calls = []

    class FakeBot:
        async def send_message(self, chat_id, text, **kwargs):
            calls.append(("message", text, kwargs))
            return SimpleNamespace(message_id=910)

        async def send_document(self, chat_id, file, **kwargs):
            calls.append(("document", str(file.path), kwargs))
            return SimpleNamespace(message_id=911)

    async def run():
        frontend = TelegramFrontend.__new__(TelegramFrontend)
        frontend.bot = FakeBot()
        frontend.backend = CodexBackend()

        async def tg_call(fn, max_retries=4):
            return await fn()

        frontend._tg_call = tg_call
        await frontend.send_assistant_reply(
            binding(tmp_path),
            ReplyEnvelope(title="回复", body="文件：[报告](<./result.pdf>)"),
        )

    import asyncio

    asyncio.run(run())

    assert calls[0][0] == "message"
    assert str(report) not in calls[0][1]
    assert calls[0][1].endswith("文件：报告")
    assert calls[1] == ("document", str(report), {"caption": "result.pdf", "message_thread_id": 456})


def test_telegram_file_upload_failure_reports_only_basename(tmp_path):
    report = tmp_path / "secret-report.pdf"
    report.write_bytes(b"pdf")
    calls = []

    class FakeBot:
        async def send_document(self, chat_id, file, **kwargs):
            raise RuntimeError("upload failed")

        async def send_message(self, chat_id, text, **kwargs):
            calls.append(text)
            return SimpleNamespace(message_id=912)

    async def run():
        frontend = TelegramFrontend.__new__(TelegramFrontend)
        frontend.bot = FakeBot()

        async def tg_call(fn, max_retries=4):
            return await fn()

        frontend._tg_call = tg_call
        result = await frontend.send_file(123, None, report, caption=report.name)
        assert result is None

    import asyncio

    asyncio.run(run())

    assert calls == ["❌ <b>附件发送失败</b>: <code>secret-report.pdf</code>"]
    assert str(tmp_path) not in calls[0]


def test_light_status_summary_does_not_use_heavy_status_injection(tmp_path, monkeypatch):
    sent = []

    async def send_html(chat_id, thread_id, html_text):
        sent.append((chat_id, thread_id, html_text))

    def fail_inject(*args, **kwargs):
        raise AssertionError("light status must not inject slash commands")

    frontend = TelegramFrontend.__new__(TelegramFrontend)
    frontend.send_html = send_html
    monkeypatch.setattr("tmuxbot.frontends.telegram.tmux_capture", lambda target, lines=50: "• Working (2s)")
    monkeypatch.setattr("tmuxbot.tmux.tmux_has_session", lambda session: True)
    monkeypatch.setattr("tmuxbot.tmux.tmux_pane_command", lambda target: "codex")
    monkeypatch.setattr("tmuxbot.commands.inject_slash_and_capture", fail_inject)

    import asyncio

    asyncio.run(frontend.send_light_status_summary(binding(tmp_path), 123, 456))

    assert sent[0][0:2] == (123, 456)
    assert "轻状态" in sent[0][2]
    assert "• Working (2s)" in sent[0][2]
    assert "/context" not in sent[0][2]
    assert "/usage" not in sent[0][2]


def test_interrupt_confirmation_buttons(tmp_path):
    calls = []

    class FakeBot:
        async def send_message(self, chat_id, text, **kwargs):
            calls.append(("message", chat_id, text, kwargs))
            return SimpleNamespace(message_id=903)

    async def run():
        frontend = TelegramFrontend.__new__(TelegramFrontend)
        frontend.bot = FakeBot()

        async def tg_call(fn, max_retries=4):
            return await fn()

        frontend._tg_call = tg_call
        await frontend.send_interrupt_confirmation(binding(tmp_path), 123, 456)

    import asyncio

    asyncio.run(run())

    markup = calls[0][3]["reply_markup"]
    labels = [button.text for row in markup.inline_keyboard for button in row]
    callbacks = [button.callback_data for row in markup.inline_keyboard for button in row]
    token = binding_token("alpha")
    assert labels == ["确认中断", "取消"]
    assert callbacks == [f"tui:{token}:ctrl_c", f"tui:{token}:refresh"]


def test_telegram_stop_always_closes_http_session():
    calls = []

    class FakeDispatcher:
        async def stop_polling(self):
            calls.append("stop_polling")
            raise RuntimeError("polling already stopped")

    class FakeSession:
        async def close(self):
            calls.append("close_session")

    async def run():
        frontend = TelegramFrontend.__new__(TelegramFrontend)
        frontend._unknown_chat_leave_tasks = {}
        frontend.dp = FakeDispatcher()
        frontend.bot = SimpleNamespace(session=FakeSession())
        await frontend.stop()

    import asyncio

    asyncio.run(run())

    assert calls == ["stop_polling", "close_session"]
