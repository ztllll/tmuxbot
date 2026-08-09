import asyncio
from pathlib import Path
from types import SimpleNamespace

from tmuxbot.core.events import ProviderEvent, ProviderEventKind
from tmuxbot.jsonl import _dispatch_provider_events
from tmuxbot.state import Binding


def binding(tmp_path: Path) -> Binding:
    return Binding(
        name="alpha",
        chat_id=123,
        thread_id=456,
        tmux_session="alpha",
        tmux_window=0,
        tmux_pane=0,
        cwd=tmp_path,
        backend="pi",
    )


def test_provider_delivery_failure_is_reported_to_tailer(tmp_path, monkeypatch):
    async def fail(*_args, **_kwargs):
        raise RuntimeError("send failed")

    monkeypatch.setattr("tmuxbot.jsonl.on_tmux_event", fail)
    event = ProviderEvent(
        event_id="pi:s1:final:1",
        kind=ProviderEventKind.FINAL_TEXT,
        text="final answer",
        provider_session_id="s1",
    )

    delivered = asyncio.run(
        _dispatch_provider_events(
            binding(tmp_path),
            [event],
            SimpleNamespace(),
            SimpleNamespace(),
            SimpleNamespace(),
        )
    )

    assert delivered is False


def test_provider_delivery_success_allows_offset_commit(tmp_path, monkeypatch):
    async def succeed(*_args, **_kwargs):
        return None

    monkeypatch.setattr("tmuxbot.jsonl.on_tmux_event", succeed)
    event = ProviderEvent(
        event_id="pi:s1:final:1",
        kind=ProviderEventKind.FINAL_TEXT,
        text="final answer",
        provider_session_id="s1",
    )

    delivered = asyncio.run(
        _dispatch_provider_events(
            binding(tmp_path),
            [event],
            SimpleNamespace(),
            SimpleNamespace(),
            SimpleNamespace(),
        )
    )

    assert delivered is True
