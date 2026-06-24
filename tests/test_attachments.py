from pathlib import Path

from tmuxbot.attachments import attachment_prompt, attachment_path, safe_filename


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
