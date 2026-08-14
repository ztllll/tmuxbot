import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from tmuxbot.core.events import ProviderEvent, ProviderEventKind
from tmuxbot.jsonl import _dispatch_provider_events, _initial_jsonl_offset, jsonl_poll_loop
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
        backend="omp",
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

    assert _initial_jsonl_offset(current, transcript, last_file=tmp_path / "old.jsonl") == 0


def test_pending_transcript_waits_without_poll_error(tmp_path, monkeypatch, caplog):

    async def stop_after_first_poll(_delay):
        raise asyncio.CancelledError

    current = binding(tmp_path)
    pending = tmp_path / "pending.jsonl"
    backend = SimpleNamespace(
        name="omp",
        poll_provider_events=lambda _binding: [],
        session_identity=lambda _binding, path: SimpleNamespace(
            session_id="pending-session",
            transcript_path=str(path),
        ),
        find_active_jsonl=lambda _binding: pending,
    )
    frontend = SimpleNamespace(bindings=[current], bindings_file=None)
    state = SimpleNamespace(bg_tasks=set(), offsets={})
    monkeypatch.setattr("tmuxbot.jsonl.asyncio.sleep", stop_after_first_poll)

    with caplog.at_level(logging.ERROR, logger="tmuxbot"):
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(
                jsonl_poll_loop(
                    current,
                    backend,
                    frontend,
                    state,
                    tmp_path / "offsets.json",
                )
            )

    assert "poll err" not in caplog.text


def test_provider_delivery_failure_is_reported_to_tailer(tmp_path, monkeypatch):
    async def fail(*_args, **_kwargs):
        raise RuntimeError("send failed")

    monkeypatch.setattr("tmuxbot.jsonl.on_tmux_event", fail)
    event = ProviderEvent(
        event_id="omp:s1:final:1",
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


def test_managed_omp_provider_error_is_deferred_but_legacy_error_is_delivered(
    tmp_path, monkeypatch
):
    delivered_events = []

    async def capture(_binding, kind, body, *_args):
        delivered_events.append((kind, body))

    monkeypatch.setattr("tmuxbot.jsonl.on_tmux_event", capture)
    event = ProviderEvent(
        event_id="omp:s1:provider_error:1",
        kind=ProviderEventKind.PROVIDER_ERROR,
        text="failed",
        provider_session_id="s1",
    )
    backend = SimpleNamespace(name="omp", provider_error_is_managed=lambda _binding: True)

    assert (
        asyncio.run(
            _dispatch_provider_events(
                binding(tmp_path), [event], SimpleNamespace(), SimpleNamespace(), backend
            )
        )
        is True
    )
    assert delivered_events == []

    backend.provider_error_is_managed = lambda _binding: False
    assert (
        asyncio.run(
            _dispatch_provider_events(
                binding(tmp_path), [event], SimpleNamespace(), SimpleNamespace(), backend
            )
        )
        is True
    )
    assert delivered_events == [("assistant_tools", "failed")]


def test_provider_delivery_success_allows_offset_commit(tmp_path, monkeypatch):
    async def succeed(*_args, **_kwargs):
        return None

    monkeypatch.setattr("tmuxbot.jsonl.on_tmux_event", succeed)
    event = ProviderEvent(
        event_id="omp:s1:final:1",
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
