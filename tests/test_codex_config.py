from pathlib import Path

from tmuxbot.providers.codex_config import codex_launch_arguments, codex_model_from_config


def test_codex_reads_top_level_model_from_config(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text('model = "gpt-5.6-sol"\n', encoding="utf-8")

    assert codex_model_from_config(config) == "gpt-5.6-sol"
    assert codex_launch_arguments(config) == (
        "--dangerously-bypass-approvals-and-sandbox",
        "-m",
        "gpt-5.6-sol",
    )


def test_codex_omits_model_flag_when_config_is_missing_or_invalid(tmp_path: Path) -> None:
    missing = tmp_path / "missing.toml"
    invalid = tmp_path / "invalid.toml"
    invalid.write_text("model = [\n", encoding="utf-8")

    assert codex_model_from_config(missing) is None
    assert codex_model_from_config(invalid) is None
    assert codex_launch_arguments(missing) == ("--dangerously-bypass-approvals-and-sandbox",)
