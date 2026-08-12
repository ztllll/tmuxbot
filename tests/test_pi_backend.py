import asyncio
import json
from pathlib import Path

import pytest

from tmuxbot.backends import pi
from tmuxbot.backends.pi import PiBackend, encode_pi_cwd
from tmuxbot.core.events import ProviderEventKind, TerminalState
from tmuxbot.state import Binding


def binding(cwd: Path) -> Binding:
    return Binding(
        name="pi-route",
        chat_id=1,
        thread_id=None,
        tmux_session="pi-route",
        tmux_window=0,
        tmux_pane=0,
        cwd=cwd,
        backend="pi",
    )


def write_session(path: Path, cwd: Path, session_id: str = "session-1") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            (
                json.dumps(
                    {
                        "type": "session",
                        "version": 3,
                        "id": session_id,
                        "cwd": str(cwd),
                    }
                ),
                json.dumps(
                    {
                        "type": "model_change",
                        "provider": "openai",
                        "modelId": "gpt-5.6-sol",
                    }
                ),
                json.dumps(
                    {
                        "type": "thinking_level_change",
                        "thinkingLevel": "high",
                    }
                ),
            )
        )
        + "\n",
        encoding="utf-8",
    )


def test_pi_cwd_encoding_matches_pi_session_manager():
    assert encode_pi_cwd(Path("/home/user/project:alpha")) == "--home-user-project-alpha--"


def test_pi_prefers_exact_live_session_file_over_stale_route_pin(tmp_path, monkeypatch):
    sessions_root = tmp_path / "sessions"
    monkeypatch.setattr(pi, "PI_SESSIONS_DIR", sessions_root)
    cwd = tmp_path / "repo"
    old = sessions_root / encode_pi_cwd(cwd) / "old.jsonl"
    live = sessions_root / encode_pi_cwd(cwd) / "live.jsonl"
    write_session(old, cwd, session_id="old")
    write_session(live, cwd, session_id="live")
    route = binding(cwd)
    route.provider_session_id = "old"
    route.transcript_path = old
    monkeypatch.setattr(pi, "provider_session_file", lambda *_args: live)

    assert PiBackend().find_active_jsonl(route) == live


def test_pi_ignores_live_session_file_with_wrong_cwd(tmp_path, monkeypatch):
    sessions_root = tmp_path / "sessions"
    monkeypatch.setattr(pi, "PI_SESSIONS_DIR", sessions_root)
    cwd = tmp_path / "repo"
    wanted = sessions_root / encode_pi_cwd(cwd) / "wanted.jsonl"
    wrong = sessions_root / encode_pi_cwd(cwd) / "wrong.jsonl"
    write_session(wanted, cwd, session_id="wanted")
    write_session(wrong, tmp_path / "other", session_id="wrong")
    route = binding(cwd)
    route.provider_session_id = "wanted"
    route.transcript_path = wanted
    monkeypatch.setattr(pi, "provider_session_file", lambda *_args: wrong)

    assert PiBackend().find_active_jsonl(route) == wanted


def test_pi_finds_only_sessions_whose_header_matches_route_cwd(tmp_path, monkeypatch):
    sessions_root = tmp_path / "sessions"
    monkeypatch.setattr(pi, "PI_SESSIONS_DIR", sessions_root)
    cwd = tmp_path / "repo"
    wanted = sessions_root / encode_pi_cwd(cwd) / "2026-session.jsonl"
    write_session(wanted, cwd)
    wrong = sessions_root / encode_pi_cwd(cwd) / "newer-wrong.jsonl"
    write_session(wrong, tmp_path / "other", session_id="wrong")
    wrong.touch()

    backend = PiBackend()

    assert backend.find_active_jsonl(binding(cwd)) == wanted
    assert backend.session_identity(binding(cwd), wanted).session_id == "session-1"


def test_pi_new_session_handoff_adopts_only_newer_matching_session(tmp_path, monkeypatch):
    sessions_root = tmp_path / "sessions"
    monkeypatch.setattr(pi, "PI_SESSIONS_DIR", sessions_root)
    cwd = tmp_path / "repo"
    old = sessions_root / encode_pi_cwd(cwd) / "old.jsonl"
    write_session(old, cwd, session_id="old")
    route = binding(cwd)
    route.provider_session_id = "old"
    route.transcript_path = old
    route.pending_session_handoff_after = old.stat().st_mtime + 0.01

    newer = sessions_root / encode_pi_cwd(cwd) / "new.jsonl"
    write_session(newer, cwd, session_id="new")
    stamp = route.pending_session_handoff_after + 1
    newer.touch()
    import os
    os.utime(newer, (stamp, stamp))

    assert PiBackend().find_active_jsonl(route) == newer


def test_pi_compact_metadata_since_reads_native_compaction_entries(tmp_path):
    transcript = tmp_path / "session.jsonl"
    prefix = json.dumps({"type": "session", "id": "session-1", "cwd": "/tmp/repo"}) + "\n"
    transcript.write_text(prefix, encoding="utf-8")
    since_byte = transcript.stat().st_size
    with transcript.open("a", encoding="utf-8") as stream:
        stream.write(
            json.dumps(
                {
                    "type": "compaction",
                    "timestamp": "2026-08-09T00:00:00Z",
                    "tokensBefore": 64000,
                    "retainedTail": [
                        {"role": "user", "content": "latest"},
                        {
                            "role": "assistant",
                            "content": [{"type": "text", "text": "done"}],
                            "usage": {
                                "input": 1200,
                                "output": 50,
                                "cacheRead": 800,
                                "cacheWrite": 0,
                            },
                        },
                    ],
                }
            )
            + "\n"
        )

    assert PiBackend().compact_metadata_since(transcript, since_byte) == {
        "preTokens": 64000,
        "postTokens": 2050,
        "durationMs": None,
        "trigger": "manual",
    }


def test_pi_parser_normalizes_text_thinking_and_tool_calls():
    row = {
        "type": "message",
        "id": "entry-1",
        "message": {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "check the repository"},
                {
                    "type": "toolCall",
                    "id": "call-1",
                    "name": "bash",
                    "arguments": {"command": "git status --short"},
                },
                {"type": "text", "text": "Ready <now>"},
            ],
            "stopReason": "stop",
        },
    }

    events = PiBackend().parse_event(json.dumps(row), "session-1")

    assert [event.kind for event in events] == [
        ProviderEventKind.TOOL_PROGRESS,
        ProviderEventKind.FINAL_TEXT,
    ]
    assert "check the repository" in events[0].text
    assert "git status --short" in events[0].text
    assert events[1].text == "Ready &lt;now&gt;"


def test_pi_estimates_compaction_eta_from_recent_session_history(tmp_path, monkeypatch):
    sessions_root = tmp_path / "sessions"
    monkeypatch.setattr(pi, "PI_SESSIONS_DIR", sessions_root)
    cwd = tmp_path / "repo"
    transcript = sessions_root / encode_pi_cwd(cwd) / "session.jsonl"
    transcript.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        {"type": "session", "version": 3, "id": "session-1", "cwd": str(cwd)},
        {"type": "message", "id": "m1", "timestamp": "2026-08-09T00:00:00Z"},
        {"type": "compaction", "id": "c1", "timestamp": "2026-08-09T00:02:00Z"},
        {"type": "message", "id": "m2", "timestamp": "2026-08-09T01:00:00Z"},
        {"type": "compaction", "id": "c2", "timestamp": "2026-08-09T01:03:20Z"},
        {"type": "message", "id": "m3", "timestamp": "2026-08-09T02:00:00Z"},
        {"type": "compaction", "id": "c3", "timestamp": "2026-08-09T02:03:00Z"},
    ]
    transcript.write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
    )

    assert PiBackend().estimated_compaction_seconds(binding(cwd)) == 180


def test_pi_parser_emits_provider_error_for_legacy_fallback_and_compaction():
    backend = PiBackend()
    error_events = backend.parse_event(
        json.dumps(
            {
                "type": "message",
                "id": "error-1",
                "message": {
                    "role": "assistant",
                    "content": [],
                    "stopReason": "error",
                    "errorMessage": "Request timed out.",
                },
            }
        ),
        "session-1",
    )
    compaction_events = backend.parse_event(
        json.dumps(
            {
                "type": "compaction",
                "id": "compact-1",
                "timestamp": "2026-08-09T07:54:15.528Z",
                "tokensBefore": 333342,
                "summary": "summary",
                "usage": {"totalTokens": 73611},
            }
        ),
        "session-1",
    )

    assert error_events[0].kind == ProviderEventKind.PROVIDER_ERROR
    assert "Request timed out" in error_events[0].text
    assert compaction_events[0].kind == ProviderEventKind.LIFECYCLE_CHANGE
    assert compaction_events[0].metadata["lifecycle"] == "compaction_end"
    assert compaction_events[0].metadata["tokens_before"] == 333342


def test_pi_parser_ignores_user_and_tool_result_messages():
    backend = PiBackend()
    for role in ("user", "toolResult"):
        assert backend.parse_event(
            json.dumps({"type": "message", "message": {"role": role, "content": []}})
        ) == []


def test_pi_runtime_metadata_and_usage_come_from_transcript(tmp_path, monkeypatch):
    sessions_root = tmp_path / "sessions"
    monkeypatch.setattr(pi, "PI_SESSIONS_DIR", sessions_root)
    cwd = tmp_path / "repo"
    transcript = sessions_root / encode_pi_cwd(cwd) / "session.jsonl"
    write_session(transcript, cwd)
    with transcript.open("a", encoding="utf-8") as stream:
        stream.write(
            json.dumps(
                {
                    "type": "message",
                    "timestamp": "2026-08-08T01:00:00Z",
                    "message": {
                        "role": "assistant",
                        "provider": "openai",
                        "model": "gpt-5.6-sol",
                        "usage": {
                            "input": 100,
                            "output": 20,
                            "cacheRead": 50,
                            "cacheWrite": 5,
                            "reasoning": 7,
                            "totalTokens": 182,
                        },
                        "content": [{"type": "text", "text": "done"}],
                    },
                }
            )
            + "\n"
        )
        stream.write(
            json.dumps(
                {
                    "type": "message",
                    "message": {
                        "role": "toolResult",
                        "usage": {
                            "input": 10,
                            "output": 2,
                            "cacheRead": 0,
                            "cacheWrite": 3,
                            "cost": {"total": 0.01},
                        },
                    },
                }
            )
            + "\n"
        )
        stream.write(
            json.dumps(
                {
                    "type": "compaction",
                    "usage": {
                        "input": 5,
                        "output": 1,
                        "cacheRead": 2,
                        "cacheWrite": 0,
                        "cost": {"total": 0.02},
                    },
                }
            )
            + "\n"
        )

    backend = PiBackend()
    route = binding(cwd)

    metadata = backend.current_runtime_metadata(route)
    assert metadata.provider == "openai"
    assert metadata.model == "gpt-5.6-sol"
    assert metadata.effort == "high"
    assert metadata.input_tokens == 115
    assert metadata.output_tokens == 23
    assert metadata.cache_read_tokens == 52
    assert metadata.cache_write_tokens == 8
    assert metadata.cache_hit_rate == 50 / 155
    assert metadata.cost_usd == pytest.approx(0.03)
    assert backend.aggregate_usage(transcript) == {
        "count": 1,
        "input": 100,
        "output": 20,
        "cache_create": 5,
        "cache_read": 50,
        "cache_hit_rate": 50 / 155,
        "last_ts": "2026-08-08T01:00:00Z",
        "model": "gpt-5.6-sol",
    }


def test_pi_runtime_metadata_cache_invalidates_when_transcript_grows(tmp_path, monkeypatch):
    sessions_root = tmp_path / "sessions"
    monkeypatch.setattr(pi, "PI_SESSIONS_DIR", sessions_root)
    cwd = tmp_path / "repo"
    transcript = sessions_root / encode_pi_cwd(cwd) / "session.jsonl"
    write_session(transcript, cwd)
    backend = PiBackend()
    route = binding(cwd)

    first = backend.current_runtime_metadata(route)
    with transcript.open("a", encoding="utf-8") as stream:
        stream.write(
            json.dumps(
                {
                    "type": "model_change",
                    "provider": "aisupertoken",
                    "modelId": "gpt-5.6-luna",
                }
            )
            + "\n"
        )
    second = backend.current_runtime_metadata(route)

    assert first.model == "gpt-5.6-sol"
    assert second.model == "gpt-5.6-luna"
    assert second.provider == "aisupertoken"


def test_pi_terminal_status_ignores_historical_working_text_below_idle_powerline_footer():
    status = PiBackend().parse_terminal_status(
        "I previously wrote about Working... in the transcript body\n"
        "and even repeated TmuxBusyTimeout diagnostics above the footer.\n"
        "todo → Reproduce false Pi Working detection\n"
        "◐ in progress\n"
        "░▒▓ 🔌 aisupertoken 🤖 gpt 5.6-sol 🧠 high "
        "📁 tmuxbot 🌿 main ⚙ bash×2 🪟 ctx 42.9%/360k "
        "🔢 ↑9.6m ↓1.1m 📦 R477.8m CH99.6% 💸 $109.35 🕒 02:41\n"
        "📄 JSONL 19.6 MB\n"
    )

    assert status is not None
    assert status.state == TerminalState.IDLE
    assert status.label == "ready"
    assert status.extension_statuses == ("📄 JSONL 19.6 MB",)


def test_pi_terminal_status_preserves_full_shared_extension_status_line():
    status = PiBackend().parse_terminal_status(
        "░▒▓ 🤖 gpt 5.6-sol\ue0b4 🌿 no-git\ue0b4 💭 thinking 🪟 ctx 74.4%/360k\ue0b4\n"
        "🔌 滴答: 任务 • 📄 JSONL 3.5 MB\n"
    )

    assert status is not None
    assert status.extension_statuses == ("🔌 滴答: 任务 • 📄 JSONL 3.5 MB",)


def test_pi_terminal_status_recognizes_custom_powerline_footer():
    status = PiBackend().parse_terminal_status(
        "⠙ Working...\n"
        "● Todos (1/4)\n"
        "├─ ✓ #1 Audit latest Pi TUI\n"
        "├─ ◐ #2 Add Pi todo coverage (writing tests) ⛓ #1\n"
        "────────────────────────────────────────\n"
        "\n"
        "────────────────────────────────────────\n"
        "░▒▓ 🔌 aisupertoken 🤖 gpt 5.6-sol 🧠 high "
        "📁 tmuxbot 🌿 main 💭 thinking 🪟 ctx 98.3%/360k "
        "🔢 ↑6.1m ↓801k 📦 R324.1m CH99.8% 💸 $0.000 🕒 02:42\n"
        "📄 JSONL 13.8 MB"
    )

    assert status is not None
    assert status.state == TerminalState.WORKING
    assert status.provider == "aisupertoken"
    assert status.model == "gpt-5.6-sol"
    assert status.effort == "high"
    assert status.cwd == "tmuxbot"
    assert status.git_branch == "main"
    assert status.context_percent == 98.3
    assert status.context_limit == 360_000
    assert status.context_used == 353_880
    assert status.input_tokens == 6_100_000
    assert status.output_tokens == 801_000
    assert status.cache_read_tokens == 324_100_000
    assert status.cache_hit_rate == pytest.approx(0.998)
    assert status.cost_usd == 0.0
    assert status.extension_statuses == ("📄 JSONL 13.8 MB",)


def test_pi_reads_dida_work_title_for_im_footer(tmp_path, monkeypatch):
    sessions_root = tmp_path / "sessions"
    monkeypatch.setattr(pi, "PI_SESSIONS_DIR", sessions_root)
    cwd = tmp_path / "repo"
    transcript = sessions_root / encode_pi_cwd(cwd) / "session.jsonl"
    write_session(transcript, cwd)
    with transcript.open("a", encoding="utf-8") as stream:
        stream.write(
            json.dumps(
                {
                    "type": "message",
                    "id": "work-switch",
                    "parentId": "session-row",
                    "message": {
                        "role": "toolResult",
                        "toolName": "todo_work",
                        "details": {
                            "works": [
                                {
                                    "id": "work-1",
                                    "title": "顶层工作标题",
                                    "selected": True,
                                }
                            ],
                            "selectedWorkId": "work-1",
                        },
                    },
                }
            )
            + "\n"
        )
        stream.write(
            json.dumps(
                {
                    "type": "message",
                    "id": "todo-result",
                    "parentId": "work-switch",
                    "message": {
                        "role": "toolResult",
                        "toolName": "todo",
                        "details": {
                            "tasks": [{"id": 1, "subject": "执行步骤", "status": "pending"}],
                            "nextId": 2,
                            "didaWorkTaskId": "work-1",
                        },
                    },
                }
            )
            + "\n"
        )

    tasks = PiBackend().read_tasks(binding(cwd))

    assert tasks.work_title == "顶层工作标题"
    assert tasks == [{"id": 1, "subject": "执行步骤", "status": "pending"}]


def test_pi_reads_pi_created_work_title_from_first_todo_create(tmp_path, monkeypatch):
    sessions_root = tmp_path / "sessions"
    monkeypatch.setattr(pi, "PI_SESSIONS_DIR", sessions_root)
    cwd = tmp_path / "repo"
    transcript = sessions_root / encode_pi_cwd(cwd) / "session.jsonl"
    write_session(transcript, cwd)
    rows = [
        {
            "type": "message",
            "id": "create-first",
            "message": {
                "role": "toolResult",
                "toolName": "todo",
                "details": {
                    "action": "create",
                    "params": {"subject": "Pi 自建顶层标题"},
                    "tasks": [{"id": 1, "subject": "Pi 自建顶层标题", "status": "pending"}],
                    "nextId": 2,
                    "didaWorkTaskId": "work-created",
                },
            },
        },
        {
            "type": "message",
            "id": "create-second",
            "parentId": "create-first",
            "message": {
                "role": "toolResult",
                "toolName": "todo",
                "details": {
                    "action": "create",
                    "params": {"subject": "第二个 Checklist"},
                    "tasks": [
                        {"id": 1, "subject": "Pi 自建顶层标题", "status": "pending"},
                        {"id": 2, "subject": "第二个 Checklist", "status": "pending"},
                    ],
                    "nextId": 3,
                    "didaWorkTaskId": "work-created",
                },
            },
        },
    ]
    with transcript.open("a", encoding="utf-8") as stream:
        stream.write("".join(json.dumps(row) + "\n" for row in rows))

    tasks = PiBackend().read_tasks(binding(cwd))

    assert tasks.work_title == "Pi 自建顶层标题"
    assert len(tasks) == 2


def test_pi_reads_latest_rpiv_todo_snapshot_from_jsonl(tmp_path, monkeypatch):
    sessions_root = tmp_path / "sessions"
    monkeypatch.setattr(pi, "PI_SESSIONS_DIR", sessions_root)
    cwd = tmp_path / "repo"
    transcript = sessions_root / encode_pi_cwd(cwd) / "session.jsonl"
    write_session(transcript, cwd)
    snapshots = [
        [
            {"id": 1, "subject": "Old", "status": "pending"},
        ],
        [
            {"id": 1, "subject": "Audit", "status": "completed"},
            {
                "id": 2,
                "subject": "Implement",
                "status": "in_progress",
                "activeForm": "implementing adapter",
                "blockedBy": [1],
                "owner": "pi",
            },
            {"id": 3, "subject": "Deploy", "status": "pending", "blockedBy": [2]},
            {"id": 4, "subject": "Removed", "status": "deleted"},
        ],
    ]
    with transcript.open("a", encoding="utf-8") as stream:
        for tasks in snapshots:
            stream.write(
                json.dumps(
                    {
                        "type": "message",
                        "message": {
                            "role": "toolResult",
                            "toolName": "todo",
                            "details": {"action": "update", "tasks": tasks, "nextId": 5},
                        },
                    }
                )
                + "\n"
            )

    tasks = PiBackend().read_tasks(binding(cwd))

    assert tasks == snapshots[-1][:-1]
    assert tasks[1]["activeForm"] == "implementing adapter"
    assert tasks[1]["blockedBy"] == [1]


def test_pi_reads_todo_snapshot_only_from_current_jsonl_branch(tmp_path, monkeypatch):
    sessions_root = tmp_path / "sessions"
    monkeypatch.setattr(pi, "PI_SESSIONS_DIR", sessions_root)
    cwd = tmp_path / "repo"
    transcript = sessions_root / encode_pi_cwd(cwd) / "session.jsonl"
    transcript.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        {"type": "session", "version": 3, "id": "session-1", "cwd": str(cwd)},
        {
            "type": "message",
            "id": "root",
            "parentId": None,
            "message": {
                "role": "toolResult",
                "toolName": "todo",
                "details": {
                    "tasks": [{"id": 1, "subject": "Root", "status": "pending"}],
                    "nextId": 2,
                },
            },
        },
        {
            "type": "message",
            "id": "abandoned",
            "parentId": "root",
            "message": {
                "role": "toolResult",
                "toolName": "todo",
                "details": {
                    "tasks": [{"id": 1, "subject": "Wrong", "status": "completed"}],
                    "nextId": 2,
                },
            },
        },
        {
            "type": "message",
            "id": "leaf",
            "parentId": "root",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "current"}]},
        },
    ]
    transcript.write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
    )

    assert PiBackend().read_tasks(binding(cwd)) == [
        {"id": 1, "subject": "Root", "status": "pending"}
    ]


def test_pi_terminal_status_recognizes_full_native_footer():
    status = PiBackend().parse_terminal_status(
        "⠧ Working...\n"
        "~/repo (main) • release audit\n"
        "↑1.3M ↓98k R19M W2.5k CH84.2% $1.234 (sub) 8.0%/1.1M (auto)        "
        "(aisupertoken) gpt-5.6-sol • high\n"
        "mail: ready scheduler: paused"
    )

    assert status is not None
    assert status.state == TerminalState.WORKING
    assert status.provider == "aisupertoken"
    assert status.model == "gpt-5.6-sol"
    assert status.effort == "high"
    assert status.cwd == "~/repo"
    assert status.git_branch == "main"
    assert status.session_name == "release audit"
    assert status.input_tokens == 1_300_000
    assert status.output_tokens == 98_000
    assert status.cache_read_tokens == 19_000_000
    assert status.cache_write_tokens == 2_500
    assert status.cache_hit_rate == pytest.approx(0.842)
    assert status.cost_usd == 1.234
    assert status.subscription is True
    assert status.extension_statuses == ("mail: ready scheduler: paused",)
    assert status.context_percent == 8.0
    assert status.context_used == 88_000
    assert status.context_limit == 1_100_000
    assert status.auto_compact is True


def test_pi_terminal_status_keeps_left_metrics_when_model_suffix_is_truncated():
    status = PiBackend().parse_terminal_status(
        "~\n↑611k ↓44k R4.9M CH0.1% 29.7%/500k (auto)  grok-4.5 •"
    )

    assert status is not None
    assert status.cwd == "~"
    assert status.input_tokens == 611_000
    assert status.output_tokens == 44_000
    assert status.cache_read_tokens == 4_900_000
    assert status.cache_hit_rate == pytest.approx(0.001)
    assert status.context_percent == 29.7
    assert status.context_used == 148_500
    assert status.context_limit == 500_000
    assert status.auto_compact is True
    assert status.model == "grok-4.5"
    assert status.effort is None


def test_pi_terminal_status_accepts_non_reasoning_model_footer():
    status = PiBackend().parse_terminal_status(
        "~/repo\n↑12 ↓3 2.0%/128k model-without-reasoning"
    )

    assert status is not None
    assert status.model == "model-without-reasoning"
    assert status.effort is None


def test_pi_ensure_running_rejects_stopped_provider_sibling(tmp_path, monkeypatch):
    route = binding(tmp_path)
    monkeypatch.setattr(pi, "tmux_has_session", lambda _session: True)
    monkeypatch.setattr(pi, "tmux_pane_command", lambda _target: "pi")
    monkeypatch.setattr(pi, "provider_tree_is_safe", lambda *_args: False)

    with pytest.raises(RuntimeError, match="contains a stopped or missing Pi"):
        asyncio.run(PiBackend().ensure_running(route))


def test_pi_ensure_running_accepts_live_provider_tree_without_proc_session_file(tmp_path, monkeypatch):
    route = binding(tmp_path)
    monkeypatch.setattr(pi, "tmux_has_session", lambda _session: True)
    monkeypatch.setattr(pi, "tmux_pane_command", lambda _target: "pi")
    monkeypatch.setattr(pi, "provider_tree_is_safe", lambda *_args: True)

    asyncio.run(PiBackend().ensure_running(route))


def test_pi_ensure_running_rejects_an_unknown_foreground_command(tmp_path, monkeypatch):
    route = binding(tmp_path)
    monkeypatch.setattr(pi, "tmux_has_session", lambda _session: True)
    monkeypatch.setattr(pi, "tmux_pane_command", lambda _target: "python3")

    with pytest.raises(RuntimeError, match="refusing to start"):
        asyncio.run(PiBackend().ensure_running(route))


def test_pi_ensure_running_fails_closed_when_tui_never_becomes_ready(tmp_path, monkeypatch):
    route = binding(tmp_path)
    commands = iter(["bash", "bash", "pi", "pi", "pi"])

    monkeypatch.setattr(pi, "tmux_has_session", lambda _session: True)
    monkeypatch.setattr(pi, "tmux_pane_command", lambda _target: next(commands, "pi"))

    async def safe_launch(*_args, **_kwargs):
        return True

    async def fast_sleep(_seconds):
        return None

    monkeypatch.setattr(pi, "tmux_safe_launch", safe_launch)
    monkeypatch.setattr(pi, "tmux_capture", lambda *_args: "starting without footer")
    monkeypatch.setattr(pi.asyncio, "sleep", fast_sleep)

    with pytest.raises(RuntimeError, match="did not become ready"):
        asyncio.run(PiBackend().ensure_running(route))


def test_pi_recover_unhealthy_pane_respawns_and_resumes_pin(tmp_path, monkeypatch):
    route = binding(tmp_path)
    route.provider_session_id = "pinned-session"
    respawns = []
    monkeypatch.setattr(pi, "tmux_pane_command", lambda _target: "pi")
    monkeypatch.setattr(pi, "provider_tree_is_safe", lambda *_args: False)
    monkeypatch.setattr(
        pi, "tmux_respawn_pane", lambda target, cwd: respawns.append((target, cwd)) or True
    )

    async def no_sleep(_seconds):
        return None

    async def ensured(_self, _binding):
        return None

    monkeypatch.setattr(pi.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(PiBackend, "ensure_running", ensured)

    assert asyncio.run(PiBackend().recover_unhealthy_pane(route)) is True
    assert respawns == [(route.tmux_target, route.cwd)]


def test_pi_reconcile_session_identity_parses_wrapped_native_session_screen(
    tmp_path, monkeypatch
):
    cwd = tmp_path / "repo"
    cwd.mkdir()
    transcript = tmp_path / "very-long-session-directory" / "session.jsonl"
    write_session(transcript, cwd, session_id="new-session")
    route = binding(cwd)
    route.provider_session_id = "old-session"
    route.pending_session_handoff_after = 123.0
    wrapped = str(transcript)
    split = len(wrapped) // 2
    pane = (
        "Session Info\n\n"
        " File:\n"
        f" {wrapped[:split]}\n"
        f" {wrapped[split:]}\n"
        " ID: new-session\n\nMessages\n"
    )

    async def send_text(*_args, **_kwargs):
        return None

    monkeypatch.setattr(pi, "tmux_send_text", send_text)
    monkeypatch.setattr(pi, "tmux_capture", lambda *_args: pane)

    assert asyncio.run(PiBackend().reconcile_session_identity(route)) is True
    assert route.provider_session_id == "new-session"
    assert route.transcript_path == transcript
    assert route.pending_session_handoff_after is None


def test_pi_command_options_only_capture_noninteractive_text_commands():
    opts = PiBackend().command_opts()

    assert set(opts) == {"/new", "/clone", "/compact", "/session"}
    assert opts["/compact"].expect_compact_done is True
    assert opts["/new"].defer_new_session_persistence is True
    assert opts["/clone"].expect_session_handoff is True
    assert PiBackend().interactive_session_handoff_commands() == frozenset(
        {"/resume", "/fork", "/import"}
    )


def test_pi_status_footer_restores_powerline_icon_semantics():
    status = PiBackend().parse_terminal_status(
        "░▒▓ 🔌 aisupertoken 🤖 gpt 5.6-sol 🧠 high "
        "📁 tmuxbot 🌿 main 🪟 ctx 39.3%/360k "
        "🔢 ↑6.5m ↓841k 📦 R337m CH99.9% 💸 $0.000 🕒 11:06\n"
        "📄 JSONL 14.5 MB"
    )

    footer = PiBackend().format_status_footer(status)

    assert footer == (
        "🔌 aisupertoken · 🤖 gpt-5.6-sol · 🧠 high · "
        "🔢 ↑6.5M ↓841k · 📦 R337M CH99.9% · 💸 $0.000 · "
        "🪟 141k/360k (39.3%) · 📁 tmuxbot · 🌿 main · 📄 JSONL 14.5 MB"
    )


def test_pi_status_footer_preserves_all_pi_specific_metrics():
    status = PiBackend().parse_terminal_status(
        "/data/project/demo (feature/pi)\n"
        "↑48k ↓2.3k R98k CH92.0% 7.3%/360k (auto) "
        "gpt-5.6-luna • medium"
    )

    footer = PiBackend().format_status_footer(status)

    assert footer is not None
    assert "🤖 gpt-5.6-luna" in footer
    assert "🧠 medium" in footer
    assert "🔢 ↑48k ↓2.3k" in footer
    assert "📦 R98k CH92.0%" in footer
    assert "🪟 26k/360k (7.3%, auto)" in footer
    assert "🌿 feature/pi" in footer
    assert "📁 /data/project/demo" in footer
