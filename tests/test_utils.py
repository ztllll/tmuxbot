import stat
from pathlib import Path

from tmuxbot.utils import (
    encode_cwd,
    render_task_footer,
    save_offsets,
    strip_handwritten_footer,
    utf16_len,
)


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


def test_render_task_footer_includes_pi_active_form_and_dependencies():
    footer = render_task_footer(
        [
            {"id": 1, "subject": "Audit", "status": "completed"},
            {
                "id": 2,
                "subject": "Implement",
                "status": "in_progress",
                "activeForm": "implementing adapter",
                "blockedBy": [1],
            },
            {"id": 3, "subject": "Deploy", "status": "pending", "blockedBy": [2]},
        ]
    )

    assert "◼ <b>#2 Implement</b> <i>(implementing adapter)</i> ⛓ #1" in footer
    assert "◻ #3 Deploy ⛓ #2" in footer
    assert "✓ <s>#1 Audit</s>" in footer


def test_render_task_footer_hides_completed_history_when_no_task_is_active():
    assert render_task_footer([
        {"subject": "历史任务", "status": "completed"},
        {"subject": "另一条历史", "status": "completed"},
    ]) == ""


def test_render_pi_task_footer_includes_work_title_like_tui():
    class Snapshot(list):
        work_title = "实现 Pi 多信号疑似失活巡检"

    footer = render_task_footer(
        Snapshot([{"id": 1, "subject": "实现 Pi 多信号疑似失活巡检", "status": "pending"}]),
        style="pi",
    )

    assert footer == (
        "● <b>实现 Pi 多信号疑似失活巡检 · Todos (0/1)</b>\n"
        "└─ ○ 实现 Pi 多信号疑似失活巡检"
    )


def test_render_pi_task_footer_matches_tui_and_keeps_completed_only_snapshot():
    footer = render_task_footer(
        [
            {"id": 1, "subject": "Audit", "status": "completed"},
            {"id": 2, "subject": "Implement", "status": "completed", "blockedBy": [1]},
        ],
        style="pi",
    )

    assert footer == (
        "○ <b>Todos (2/2)</b>\n"
        "├─ ✓ #1 <s>Audit</s>\n"
        "└─ ✓ #2 <s>Implement</s> ⛓ #1"
    )


def test_render_pi_task_footer_preserves_snapshot_order_and_active_form():
    footer = render_task_footer(
        [
            {"id": 1, "subject": "Audit", "status": "completed"},
            {
                "id": 2,
                "subject": "Implement",
                "status": "in_progress",
                "activeForm": "implementing adapter",
                "blockedBy": [1],
            },
            {"id": 3, "subject": "Deploy", "status": "pending", "blockedBy": [2]},
        ],
        style="pi",
    )

    assert footer == (
        "● <b>Todos (1/3)</b>\n"
        "├─ ✓ #1 <s>Audit</s>\n"
        "├─ ◐ #2 <b>Implement</b> <i>(implementing adapter)</i> ⛓ #1\n"
        "└─ ○ #3 Deploy ⛓ #2"
    )


def test_save_offsets_keeps_runtime_state_private(tmp_path):
    path = tmp_path / "state/offsets.json"

    save_offsets(path, {"session.jsonl": 123}, force=True)

    assert path.read_text(encoding="utf-8").strip().endswith("123\n}")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
