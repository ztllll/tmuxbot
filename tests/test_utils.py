from pathlib import Path

from tmuxbot.utils import encode_cwd, render_task_footer, strip_handwritten_footer, utf16_len


def test_encode_cwd_matches_non_alnum_replacement():
    encoded = encode_cwd(Path("/tmp/claude_project/中文.a"))
    assert encoded.endswith("-tmp-claude-project----a")


def test_utf16_len_counts_emoji_as_two_units():
    assert utf16_len("a") == 1
    assert utf16_len("中") == 1
    assert utf16_len("👀") == 2


def test_strip_handwritten_footer_removes_task_block():
    text = "real answer\n\n━━━ 任务 ━━━\nfake task"
    assert strip_handwritten_footer(text) == "real answer"


def test_render_task_footer_hides_completed_history_when_no_task_is_active():
    assert render_task_footer([
        {"subject": "历史任务", "status": "completed"},
        {"subject": "另一条历史", "status": "completed"},
    ]) == ""
