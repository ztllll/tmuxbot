from pathlib import Path

from tmuxbot.attachments import (
    attachment_prompt,
    attachment_path,
    safe_filename,
    split_outbound_attachments,
)


def test_safe_filename_strips_paths_and_unsafe_chars():
    assert safe_filename("../../中文 report?.png", "fallback.bin") == "_ report_.png"
    assert safe_filename("..", "fallback.bin") == "fallback.bin"
    assert safe_filename("", "fallback.bin") == "fallback.bin"


def test_attachment_path_uses_shared_tmp_dir(monkeypatch, tmp_path):
    import tmuxbot.attachments as attachments

    monkeypatch.setattr(attachments, "ATTACHMENT_DIR", tmp_path)

    path = attachment_path("telegram", 42, "abc/def", "../report.txt")

    assert path == tmp_path / "telegram_42_def_report.txt"
    assert path.parent.exists()


def test_attachment_prompt_uses_caption_or_default():
    path = Path("/tmp/tmuxbot-attachments/file.txt")

    assert attachment_prompt("看这个", [path]) == f"看这个\n\n@{path}"
    assert attachment_prompt("", [path]) == f"请处理这个文件\n\n@{path}"


def test_attachment_prompt_for_codex_uses_tool_instructions():
    image = Path("/tmp/tmuxbot-attachments/image.jpg")
    report = Path("/tmp/tmuxbot-attachments/report.pdf")

    prompt = attachment_prompt("看一下", [image, report], backend_name="codex")

    assert "@/tmp/tmuxbot-attachments/image.jpg" not in prompt
    assert str(image) in prompt
    assert str(report) in prompt
    assert "view_image" in prompt
    assert "读取" in prompt


def test_split_outbound_attachments_removes_existing_path_lines(tmp_path):
    image = tmp_path / "chart.png"
    image.write_bytes(b"png")
    report = tmp_path / "report.pdf"
    report.write_bytes(b"pdf")

    text, attachments = split_outbound_attachments(
        f"生成好了:\n- @{image}\nfile://{report}\n不存在的保留: /tmp/no-such-file.txt"
    )

    assert text == "生成好了:\n不存在的保留: /tmp/no-such-file.txt"
    assert [(a.path, a.kind) for a in attachments] == [
        (image, "image"),
        (report, "file"),
    ]


def test_split_outbound_attachments_recognizes_tmux_guttered_path_lines(tmp_path):
    image = tmp_path / "screen.jpg"
    image.write_bytes(b"jpg")

    text, attachments = split_outbound_attachments(
        f"screen:\n│ @{image}\n› done"
    )

    assert text == "screen:\n› done"
    assert [(a.path, a.kind) for a in attachments] == [(image, "image")]
