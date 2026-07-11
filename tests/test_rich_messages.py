from pathlib import Path

from tmuxbot.core.events import TerminalState, TerminalStatus
from tmuxbot.core.replies import ReplyEnvelope
from tmuxbot.core.rich_messages import (
    build_reply_document,
    render_telegram_document,
    reply_summary,
)
from tmuxbot.state import Binding


def binding(tmp_path: Path) -> Binding:
    return Binding(
        name="alpha",
        chat_id=123,
        thread_id=None,
        tmux_session="alpha",
        tmux_window=0,
        tmux_pane=0,
        cwd=tmp_path,
        backend="codex",
    )


def test_build_reply_document_parses_common_blocks_without_provider_tail_heuristics(tmp_path):
    document = build_reply_document(
        binding(tmp_path),
        ReplyEnvelope(
            title="回复",
            body=(
                "## 结论\n\n"
                "普通段落\n\n"
                "- 第一项\n- 第二项\n\n"
                "> 详细信息\n\n"
                "```python\nprint(1)\n```\n\n"
                "claude-opus-4-7"
            ),
            actions=("screen", "status"),
        ),
        footer_text="ready",
    )

    assert [block.kind for block in document.blocks] == [
        "heading",
        "paragraph",
        "list",
        "quote",
        "code",
        "paragraph",
    ]
    assert document.blocks[4].language == "python"
    assert document.blocks[-1].text == "claude-opus-4-7"
    assert document.provider == "codex"
    assert document.actions == ("screen", "status")


def test_build_reply_document_display_state_overrides_terminal_state(tmp_path):
    document = build_reply_document(
        binding(tmp_path),
        ReplyEnvelope(
            title="回复",
            body="完成",
            footer=TerminalStatus(state=TerminalState.WORKING),
            metadata={"display_state": "completed"},
        ),
    )

    assert document.state == "completed"


def test_render_telegram_document_uses_balanced_native_blocks(tmp_path):
    document = build_reply_document(
        binding(tmp_path),
        ReplyEnvelope(
            title="回复",
            body="## 结论\n\n> 很长的细节\n\n```python\nprint(1)\n```",
        ),
        footer_text="Working",
    )

    result = render_telegram_document(document, full_output_threshold=8000)

    assert result.chat_html.startswith("💬 <b>回复</b> · <code>alpha</code>")
    assert "<b>结论</b>" in result.chat_html
    assert "<blockquote expandable>很长的细节</blockquote>" in result.chat_html
    assert '<pre><code class="language-python">print(1)</code></pre>' in result.chat_html
    assert result.chat_html.endswith("<i>Working</i>")


def test_reply_summary_removes_markup_and_never_uses_local_path(tmp_path):
    document = build_reply_document(
        binding(tmp_path),
        ReplyEnvelope(title="回复", body="## 完成\n\n文件已作为附件发送。"),
    )

    assert reply_summary(document) == "完成 文件已作为附件发送。"


def test_telegram_renderer_escapes_unknown_angle_brackets_but_keeps_known_tags(tmp_path):
    document = build_reply_document(
        binding(tmp_path),
        ReplyEnvelope(
            title="回复",
            body="状态 <- previous，<b>安全粗体</b>，<danger>不是标签</danger>",
        ),
    )

    result = render_telegram_document(document, full_output_threshold=8000)

    assert "状态 &lt;- previous" in result.chat_html
    assert "<b>安全粗体</b>" in result.chat_html
    assert "&lt;danger&gt;不是标签&lt;/danger&gt;" in result.chat_html
