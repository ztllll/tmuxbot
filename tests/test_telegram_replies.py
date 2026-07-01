from types import SimpleNamespace

from tmuxbot.command_adapter import binding_token
from tmuxbot.frontends.telegram import TelegramFrontend
from tmuxbot.replies import html_to_plain_text, render_assistant_reply
from tmuxbot.state import Binding


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
        "## 结论\n\n```python\nprint(1)\n```",
        full_output_threshold=8000,
        screen_footer="• Working (9s • esc to interrupt)",
    )

    assert result.chat_html.startswith("💬 <b>回复</b> · <code>alpha</code>")
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
        body,
        full_output_threshold=200,
    )

    assert "完整输出已附为文件" in result.chat_html
    assert len(result.chat_html) < len(body)
    assert result.full_text == html_to_plain_text(body)


def test_telegram_assistant_reply_sends_buttons_and_full_output_file(tmp_path):
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

        await frontend.send_assistant_reply(
            binding(tmp_path),
            "长内容\n" * 2000,
            attachments=[],
        )

    import asyncio

    asyncio.run(run())

    assert calls[0][0] == "message"
    assert calls[0][3]["message_thread_id"] == 456
    markup = calls[0][3]["reply_markup"]
    labels = [button.text for row in markup.inline_keyboard for button in row]
    assert labels == ["屏幕", "状态", "取消", "强制中断"]
    callbacks = [button.callback_data for row in markup.inline_keyboard for button in row]
    token = binding_token("alpha")
    assert callbacks == [
        f"tui:{token}:refresh",
        f"tui:{token}:status",
        f"tui:{token}:esc",
        f"tui:{token}:ctrl_c",
    ]
    assert calls[1][0] == "document"
    assert calls[1][2] == "assistant-alpha.txt"
