"""Channel-neutral control-panel policy and persistence helpers."""
from __future__ import annotations

import html
import os
import tempfile
import threading
from pathlib import Path

import yaml

from tmuxbot.state import Binding


_CONTROL_COMMANDS = frozenset({"/menu", "/panel", "/settings", "/mention"})
_PANEL_ACTION_COMMANDS = {
    "cmd_status": "/status",
    "cmd_screen": "/screen",
    "cmd_new": "/new",
    "cmd_compact": "/compact",
    "cmd_resume": "/resume",
    "cmd_model": "/model",
    "cmd_esc": "/esc",
    "cmd_cc": "/cc",
    "cmd_restart": "/restart",
}
_PANEL_WRITE_LOCK = threading.Lock()


def _command_name(text: str) -> str:
    token = text.strip().split(maxsplit=1)[0] if text.strip() else ""
    return token.split("@", 1)[0].lower()


def is_control_command(text: str) -> bool:
    return _command_name(text) in _CONTROL_COMMANDS


def parse_mention_command(text: str) -> bool | None | str:
    parts = text.strip().split()
    if not parts or _command_name(text) != "/mention":
        return "invalid"
    if len(parts) == 1 or parts[1].lower() == "status":
        return "status"
    value = parts[1].lower()
    if value == "on":
        return False
    if value == "off":
        return True
    if value == "default":
        return None
    return "invalid"


def panel_command_for_action(action: str) -> str | None:
    return _PANEL_ACTION_COMMANDS.get(action)


def effective_mention_required(binding: Binding, frontend_default: bool) -> bool:
    override = getattr(binding, "mention_required", None)
    if override is None:
        return frontend_default
    return bool(override)


def mention_policy_source(binding: Binding) -> str:
    return "部署默认" if getattr(binding, "mention_required", None) is None else "binding 覆盖"


def render_panel_text(
    binding: Binding,
    *,
    frontend_default: bool,
    runtime_mode: str | None = None,
    current_model: str | None = None,
) -> str:
    required = effective_mention_required(binding, frontend_default)
    policy = "必须 @机器人" if required else "无需 @机器人"
    provider = "Codex" if binding.backend == "codex" else "Claude"
    runtime = runtime_mode or os.getenv("TMUXBOT_RUNTIME_V2", "off")
    model = html.escape(current_model) if current_model else "暂未读取到（发送一次任务后自动可见）"
    return "\n".join(
        [
            "🎛 <b>tmuxbot 控制面板</b>",
            "所有操作都会作用于当前 tmux 内的真实 CLI 会话。",
            "",
            f"会话: <code>{html.escape(binding.name)}</code>",
            f"通道: <code>{html.escape(binding.channel)}</code> · Provider: <b>{provider}</b>",
            f"tmux: <code>{html.escape(binding.tmux_target)}</code>",
            f"Runtime V2: <code>{html.escape(runtime)}</code>",
            f"群聊唤醒: <b>当前{policy}</b>（{mention_policy_source(binding)}）",
            f"当前模型: <code>{model}</code>",
            "",
            "🧠 点“切换模型”会打开当前 CLI 的原生 /model 选择器；候选由 CLI 实时提供，不写死候选模型。",
            "选择后会保留当前会话上下文；可刷新 /menu 或用 /status 确认当前模型。",
            "⚠️ /new 会创建新会话；普通助手回复仍保持无按钮。",
        ]
    )


def save_binding_mention_policy(
    bindings_file: Path | None,
    binding: Binding,
    value: bool | None,
) -> None:
    if bindings_file is None:
        binding.mention_required = value
        return
    with _PANEL_WRITE_LOCK:
        raw = yaml.safe_load(bindings_file.read_text(encoding="utf-8")) or {}
        found = False
        for entry in raw.get("bindings", []):
            if entry.get("name") != binding.name:
                continue
            found = True
            if value is None:
                entry.pop("mention_required", None)
            else:
                entry["mention_required"] = value
            break
        if not found:
            raise ValueError(f"binding not found: {binding.name}")
        rendered = yaml.safe_dump(raw, allow_unicode=True, sort_keys=False)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=bindings_file.parent,
            prefix=f".{bindings_file.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            handle.write(rendered)
            temp_path = Path(handle.name)
        os.replace(temp_path, bindings_file)
        binding.mention_required = value
