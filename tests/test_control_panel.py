from pathlib import Path

import yaml

from tmuxbot.control_panel import (
    effective_mention_required,
    is_control_command,
    parse_mention_command,
    panel_command_for_action,
    render_panel_text,
    save_binding_mention_policy,
)
from tmuxbot.state import Binding


def binding(tmp_path: Path, *, mention_required=None) -> Binding:
    return Binding(
        name="alpha",
        chat_id=-100,
        thread_id=7,
        tmux_session="alpha",
        tmux_window=0,
        tmux_pane=0,
        cwd=tmp_path,
        backend="codex",
        channel="telegram",
        mention_required=mention_required,
    )


def test_effective_mention_policy_uses_binding_override_then_frontend_default(tmp_path):
    assert effective_mention_required(binding(tmp_path, mention_required=True), False)
    assert not effective_mention_required(binding(tmp_path, mention_required=False), True)
    assert effective_mention_required(binding(tmp_path), True)
    assert not effective_mention_required(binding(tmp_path), False)


def test_parse_mention_command_and_control_command_detection():
    assert parse_mention_command("/mention on") is False
    assert parse_mention_command("/mention off") is True
    assert parse_mention_command("/mention default") is None
    assert parse_mention_command("/mention status") == "status"
    assert parse_mention_command("/mention") == "status"
    assert parse_mention_command("/mention bad") == "invalid"
    assert is_control_command("/menu")
    assert is_control_command("/panel")  # 旧命令保留兼容
    assert is_control_command("/settings@my_bot")
    assert is_control_command("/mention off")
    assert not is_control_command("/status")
    assert panel_command_for_action("cmd_restart") == "/restart"


def test_render_panel_text_is_chinese_and_explains_native_model_picker(tmp_path):
    text = render_panel_text(
        binding(tmp_path, mention_required=False),
        frontend_default=True,
        runtime_mode="on",
    )

    assert "控制面板" in text
    assert "当前无需 @机器人" in text
    assert "binding 覆盖" in text
    assert "alpha:0.0" in text
    assert "Codex" in text
    assert "原生 /model" in text
    assert "当前模型" in text
    assert "tmux" in text


def test_render_panel_text_includes_the_runtime_discovered_model(tmp_path):
    text = render_panel_text(
        binding(tmp_path),
        frontend_default=False,
        current_model="gpt-5.6-terra",
    )

    assert "gpt-5.6-terra" in text
    assert "不写死候选模型" in text


def test_save_binding_mention_policy_updates_yaml_atomically(tmp_path):
    path = tmp_path / "bindings.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "bindings": [
                    {
                        "name": "alpha",
                        "chat_id": -100,
                        "tmux_session": "alpha",
                        "cwd": str(tmp_path),
                    }
                ]
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    b = binding(tmp_path)

    save_binding_mention_policy(path, b, True)
    assert b.mention_required is True
    assert yaml.safe_load(path.read_text())["bindings"][0]["mention_required"] is True

    save_binding_mention_policy(path, b, None)
    assert b.mention_required is None
    assert "mention_required" not in yaml.safe_load(path.read_text())["bindings"][0]
