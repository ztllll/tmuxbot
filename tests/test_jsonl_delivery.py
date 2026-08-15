import asyncio
from pathlib import Path
from types import SimpleNamespace

from tmuxbot.core.events import ProviderEvent, ProviderEventKind
from tmuxbot.jsonl import _dispatch_provider_events, _initial_jsonl_offset
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


def test_new_unpinned_route_reads_first_transcript_from_zero(tmp_path):
    transcript = tmp_path / "first.jsonl"
    transcript.write_text("assistant first reply\n", encoding="utf-8")
    current = binding(tmp_path)

    assert _initial_jsonl_offset(current, transcript, last_file=None) == 0


def test_existing_pinned_route_bootstraps_at_end(tmp_path):
    transcript = tmp_path / "existing.jsonl"
    transcript.write_text("historical assistant reply\n", encoding="utf-8")
    current = binding(tmp_path)
    current.provider_session_id = "existing"
    current.transcript_path = transcript

    assert _initial_jsonl_offset(current, transcript, last_file=None) == transcript.stat().st_size


def test_running_session_switch_reads_new_transcript_from_zero(tmp_path):
    transcript = tmp_path / "new.jsonl"
    transcript.write_text("first reply after new\n", encoding="utf-8")
    current = binding(tmp_path)

    assert _initial_jsonl_offset(
        current, transcript, last_file=tmp_path / "old.jsonl"
    ) == 0


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


def test_managed_pi_provider_error_is_deferred_but_legacy_error_is_delivered(
    tmp_path, monkeypatch
):
    delivered_events = []

    async def capture(_binding, kind, body, *_args):
        delivered_events.append((kind, body))

    monkeypatch.setattr("tmuxbot.jsonl.on_tmux_event", capture)
    event = ProviderEvent(
        event_id="pi:s1:provider_error:1",
        kind=ProviderEventKind.PROVIDER_ERROR,
        text="failed",
        provider_session_id="s1",
    )
    backend = SimpleNamespace(name="pi", provider_error_is_managed=lambda _binding: True)

    assert asyncio.run(
        _dispatch_provider_events(
            binding(tmp_path), [event], SimpleNamespace(), SimpleNamespace(), backend
        )
    ) is True
    assert delivered_events == []

    backend.provider_error_is_managed = lambda _binding: False
    assert asyncio.run(
        _dispatch_provider_events(
            binding(tmp_path), [event], SimpleNamespace(), SimpleNamespace(), backend
        )
    ) is True
    assert delivered_events == [("provider_error", "failed")]


def test_interaction_and_provider_error_keep_distinct_attention_routes(tmp_path, monkeypatch):
    delivered_events = []

    async def capture(_binding, kind, body, *_args):
        delivered_events.append((kind, body))

    monkeypatch.setattr("tmuxbot.jsonl.on_tmux_event", capture)
    events = [
        ProviderEvent(
            event_id="claude:s1:interaction:1",
            kind=ProviderEventKind.INTERACTION_REQUEST,
            text="请选择目标环境",
            provider_session_id="s1",
        ),
        ProviderEvent(
            event_id="claude:s1:error:1",
            kind=ProviderEventKind.PROVIDER_ERROR,
            text="授权失败",
            provider_session_id="s1",
        ),
    ]

    assert asyncio.run(
        _dispatch_provider_events(
            binding(tmp_path), events, SimpleNamespace(), SimpleNamespace(), SimpleNamespace()
        )
    ) is True
    assert delivered_events == [
        ("interaction_request", "请选择目标环境"),
        ("provider_error", "授权失败"),
    ]


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
