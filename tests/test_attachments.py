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


def test_split_outbound_attachments_recognizes_markdown_file_link(tmp_path):
    final = tmp_path / "终稿.md"
    final.write_text("# done\n", encoding="utf-8")

    text, attachments = split_outbound_attachments(
        f"这是终稿文件路径:\n\n[终稿.md](<{final}>)"
    )

    assert text == "这是终稿文件路径:"
    assert [(a.path, a.kind) for a in attachments] == [(final, "file")]


def test_split_outbound_attachments_promotes_inline_links_and_images(tmp_path):
    report = tmp_path / "report final.pdf"
    report.write_bytes(b"pdf")
    image = tmp_path / "chart.png"
    image.write_bytes(b"png")

    text, attachments = split_outbound_attachments(
        f"报告在这里：[终稿](<{report}>)，图表见 ![趋势图](<{image}>)。",
        cwd=tmp_path,
    )

    assert text == "报告在这里：终稿，图表见 趋势图。"
    assert [(a.path, a.kind, a.label) for a in attachments] == [
        (report, "file", "终稿"),
        (image, "image", "趋势图"),
    ]


def test_split_outbound_attachments_strips_editor_line_suffixes(tmp_path):
    report = tmp_path / "report.md"
    report.write_text("done", encoding="utf-8")

    text, attachments = split_outbound_attachments(
        f"[第一处](<{report}:12>) 和 [第二处](<{report}#L20>)",
        cwd=tmp_path,
    )

    assert text == "第一处 和 第二处"
    assert [(a.path, a.label) for a in attachments] == [(report, "第一处")]


def test_split_outbound_attachments_resolves_relative_paths_from_binding_cwd(tmp_path):
    report = tmp_path / "build" / "result.csv"
    report.parent.mkdir()
    report.write_text("a,b\n", encoding="utf-8")

    text, attachments = split_outbound_attachments(
        "生成完成：\n./build/result.csv",
        cwd=tmp_path,
    )

    assert text == "生成完成："
    assert [(a.path, a.kind) for a in attachments] == [(report, "file")]


def test_split_outbound_attachments_rejects_existing_files_outside_allowed_roots(tmp_path):
    outside = Path("/etc/hosts")
    if not outside.is_file():
        return

    text, attachments = split_outbound_attachments(str(outside), cwd=tmp_path)

    assert text == str(outside)
    assert attachments == []


def test_split_outbound_attachments_deduplicates_path_and_keeps_first_label(tmp_path):
    report = tmp_path / "result.pdf"
    report.write_bytes(b"pdf")

    text, attachments = split_outbound_attachments(
        f"文件：[结果](<{report}>)\n@{report}",
        cwd=tmp_path,
    )

    assert text == "文件：结果"
    assert [(a.path, a.label) for a in attachments] == [(report, "结果")]
