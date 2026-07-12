import asyncio
from pathlib import Path
from types import SimpleNamespace

from tmuxbot.backends.codex import CodexBackend
from tmuxbot.frontends.telegram import (
    TelegramFrontend,
    build_telegram_panel_markup,
)
from tmuxbot.state import Binding


def binding(tmp_path: Path) -> Binding:
    return Binding(
        name="alpha",
        chat_id=-100,
        thread_id=None,
        tmux_session="alpha",
        tmux_window=0,
        tmux_pane=0,
        cwd=tmp_path,
        backend="codex",
        channel="telegram",
        mention_required=True,
    )


def test_telegram_panel_markup_is_chinese_and_contains_common_commands(tmp_path):
    markup = build_telegram_panel_markup(binding(tmp_path))
    labels = [button.text for row in markup.inline_keyboard for button in row]
    callbacks = [button.callback_data for row in markup.inline_keyboard for button in row]

    assert labels == [
        "无需 @",
        "必须 @",
        "继承默认",
        "状态",
        "屏幕",
        "新会话",
        "压缩上下文",
        "恢复会话",
        "切换模型",
        "Esc",
        "Ctrl-C",
        "重启 CLI",
        "刷新",
        "关闭",
    ]
    assert any(value.endswith(":cmd_model") for value in callbacks)
    assert any(value.endswith(":confirm_new") for value in callbacks)
    assert any(value.endswith(":confirm_restart") for value in callbacks)


def test_telegram_panel_new_session_uses_confirmation_keyboard(tmp_path):
    markup = build_telegram_panel_markup(binding(tmp_path), confirm_new=True)
    labels = [button.text for row in markup.inline_keyboard for button in row]

    assert labels == ["确认创建新会话", "返回面板"]


def test_telegram_panel_restart_uses_confirmation_keyboard(tmp_path):
    markup = build_telegram_panel_markup(binding(tmp_path), confirm_restart=True)
    labels = [button.text for row in markup.inline_keyboard for button in row]

    assert labels == ["确认重启 CLI", "返回面板"]


def test_telegram_send_control_panel_uses_chinese_text_and_keyboard(tmp_path):
    calls = []

    class FakeBot:
        async def send_message(self, chat_id, text, **kwargs):
            calls.append((chat_id, text, kwargs))
            return SimpleNamespace(message_id=1)

    frontend = TelegramFrontend.__new__(TelegramFrontend)
    frontend.bot = FakeBot()
    frontend.backend = CodexBackend()
    frontend.group_only_when_mentioned = True

    async def tg_call(fn, max_retries=4):
        return await fn()

    frontend._tg_call = tg_call

    asyncio.run(frontend.send_control_panel(binding(tmp_path), -100, None))

    assert "tmuxbot 控制面板" in calls[0][1]
    assert "当前模型" in calls[0][1]
    assert "原生 /model" in calls[0][1]
    assert calls[0][2]["reply_markup"].inline_keyboard


def test_telegram_panel_model_action_dispatches_native_model_command(tmp_path, monkeypatch):
    calls = []

    async def dispatch(*args, **kwargs):
        calls.append((args, kwargs))

    monkeypatch.setattr("tmuxbot.dispatch.dispatch_incoming_text", dispatch)
    frontend = TelegramFrontend.__new__(TelegramFrontend)
    frontend.backend = CodexBackend()
    frontend.state = SimpleNamespace()
    frontend._bot_username = "tmuxbot"
    b = binding(tmp_path)

    asyncio.run(frontend.execute_panel_command(b, -100, None, "cmd_model"))

    assert calls[0][0][6] == "/model"


def test_telegram_claude_model_interaction_offers_session_only_button(tmp_path):
    calls = []

    class FakeBot:
        async def send_message(self, chat_id, text, **kwargs):
            calls.append(kwargs["reply_markup"])
            return SimpleNamespace(message_id=2)

    frontend = TelegramFrontend.__new__(TelegramFrontend)
    frontend.bot = FakeBot()
    frontend.backend = SimpleNamespace(name="claude_code")

    async def tg_call(fn, max_retries=4):
        return await fn()

    frontend._tg_call = tg_call

    asyncio.run(
        frontend.send_interaction_card(-100, None, "🎛 /model 已注入", "alpha")
    )

    labels = [button.text for row in calls[0].inline_keyboard for button in row]
    assert "仅本会话" in labels
