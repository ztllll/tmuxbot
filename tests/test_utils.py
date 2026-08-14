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


def test_render_task_footer_includes_omp_active_form_and_dependencies():
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
    assert (
        render_task_footer(
            [
                {"subject": "历史任务", "status": "completed"},
                {"subject": "另一条历史", "status": "completed"},
            ]
        )
        == ""
    )


def test_render_omp_task_footer_groups_phases_and_shows_blocker():
    footer = render_task_footer(
        [
            {"phase": "Implementation", "content": "Audit", "status": "completed"},
            {"phase": "Implementation", "content": "Implement", "status": "in_progress"},
            {
                "phase": "Implementation",
                "content": "Deploy",
                "status": "blocked",
                "blocker": "Need <approval>",
            },
            {"phase": "Implementation", "content": "Dropped", "status": "abandoned"},
            {"phase": "Verification", "content": "Smoke", "status": "pending"},
        ],
        style="omp",
    )

    assert footer == (
        "● <b>Todos (1/4)</b>\n"
        "▾ <b>Implementation</b> (1/3)\n"
        "├─ ✓ <s>Audit</s>\n"
        "├─ ◐ <b>Implement</b>\n"
        "└─ ⛔ Deploy — <i>Need &lt;approval&gt;</i>\n"
        "▾ <b>Verification</b> (0/1)\n"
        "└─ ○ Smoke"
    )
    assert "Dropped" not in footer


def test_render_omp_task_footer_keeps_completed_only_and_hides_abandoned_only():
    assert render_task_footer(
        [{"phase": "Done", "content": "Audit", "status": "completed"}],
        style="omp",
    ) == ("○ <b>Todos (1/1)</b>\n▾ <b>Done</b> (1/1)\n└─ ✓ <s>Audit</s>")
    assert (
        render_task_footer(
            [{"phase": "Old", "content": "Dropped", "status": "abandoned"}],
            style="omp",
        )
        == ""
    )


def test_save_offsets_keeps_runtime_state_private(tmp_path):
    path = tmp_path / "state/offsets.json"

    save_offsets(path, {"session.jsonl": 123}, force=True)

    assert path.read_text(encoding="utf-8").strip().endswith("123\n}")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
