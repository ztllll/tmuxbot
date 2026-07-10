import asyncio
from pathlib import Path
from types import SimpleNamespace

from tmuxbot.backends.codex import CodexBackend
from tmuxbot.core.events import TerminalState, TerminalStatus
from tmuxbot.core.replies import ReplyEnvelope
from tmuxbot.frontends.feishu import FeishuFrontend
from tmuxbot.frontends.telegram import TelegramFrontend
from tmuxbot.state import Binding


def _binding(tmp_path: Path, channel: str) -> Binding:
    return Binding(
        name=f"alpha-{channel}",
        chat_id="oc_123" if channel == "feishu" else 123,
        thread_id=None,
        tmux_session="alpha",
        tmux_window=0,
        tmux_pane=0,
        cwd=tmp_path,
        backend="codex",
        channel=channel,
    )


def _envelope(attachment: Path) -> ReplyEnvelope:
    return ReplyEnvelope(
        title="回复",
        body="## 结论\n\n完成",
        footer=TerminalStatus(
            state=TerminalState.WORKING,
            label="Working",
            model="gpt-5",
            effort="high",
            duration_seconds=9,
        ),
        attachments=(str(attachment),),
        actions=("screen", "status", "cancel", "interrupt"),
    )


def test_telegram_and_feishu_share_reply_envelope_semantics(tmp_path, monkeypatch):
    attachment = tmp_path / "result.txt"
    attachment.write_text("full", encoding="utf-8")
    envelope = _envelope(attachment)
    tg_calls = []
    fs_calls = []

    class FakeTelegramBot:
        async def send_message(self, chat_id, text, **kwargs):
            tg_calls.append(("message", text, kwargs))
            return SimpleNamespace(message_id=11)

    async def run():
        telegram = TelegramFrontend.__new__(TelegramFrontend)
        telegram.bot = FakeTelegramBot()
        telegram.backend = CodexBackend()

        async def tg_call(fn, max_retries=4):
            return await fn()

        telegram._tg_call = tg_call
        telegram.send_file = lambda *args, **kwargs: _record_async(fs_calls, "tg-file")
        telegram.send_image = lambda *args, **kwargs: _record_async(fs_calls, "tg-image")

        feishu = FeishuFrontend.__new__(FeishuFrontend)
        feishu.backend = CodexBackend()
        feishu._outbound_message_ids = set()
        feishu._send_card_sync = lambda chat_id, md: fs_calls.append(("card", md)) or "fs-11"
        feishu.send_file = lambda *args, **kwargs: _record_async(fs_calls, "fs-file")
        feishu.send_image = lambda *args, **kwargs: _record_async(fs_calls, "fs-image")

        tg_result = await telegram.send_assistant_reply(_binding(tmp_path, "telegram"), envelope)
        fs_result = await feishu.send_assistant_reply(_binding(tmp_path, "feishu"), envelope)
        return tg_result, fs_result

    monkeypatch.setattr(
        "tmuxbot.frontends.telegram.tmux_capture",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("reply rendering must not capture tmux in the channel adapter")
        ),
    )
    tg_result, fs_result = asyncio.run(run())

    assert tg_result.message_id == 11
    assert fs_result.message_id == "fs-11"
    assert "结论" in tg_calls[0][1]
    feishu_card = next(call for call in fs_calls if call[0] == "card")
    assert "结论" in feishu_card[1]
    assert "gpt-5 high" in tg_calls[0][1]
    assert "gpt-5 high" in feishu_card[1]
    assert any(call[0] == "tg-file" for call in fs_calls)
    assert any(call[0] == "fs-file" for call in fs_calls)


async def _record_async(calls, kind):
    calls.append((kind,))
    return SimpleNamespace(message_id=f"{kind}-1")


def test_channel_capabilities_describe_real_adapter_features():
    assert TelegramFrontend.capabilities.supports_actions
    assert TelegramFrontend.capabilities.supports_threads
    assert FeishuFrontend.capabilities.supports_cards
    assert FeishuFrontend.capabilities.supports_edit
