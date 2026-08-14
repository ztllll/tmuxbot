import json
import shlex
from pathlib import Path

import pytest
import yaml

from tmuxbot.backends import omp
from tmuxbot.backends.omp import OmpBackend
from tmuxbot.core.events import ProviderEventKind, TerminalState, TerminalStatus
from tmuxbot.runtime.omp_plan_mode import current_jsonl_branch
from tmuxbot.state import Binding


def binding(cwd: Path, transcript: Path | None = None) -> Binding:
    route = Binding(
        name="omp-route",
        chat_id=1,
        thread_id=None,
        tmux_session="omp-route",
        tmux_window=0,
        tmux_pane=0,
        cwd=cwd,
        backend="omp",
    )
    route.transcript_path = transcript
    return route


def write_transcript(
    path: Path,
    cwd: Path,
    rows: list[dict],
    *,
    session_id: str = "session-1",
    slot_title: str = "Slot title",
    header_title: str = "Header title",
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = [
        json.dumps({"type": "title", "v": 1, "title": slot_title}),
        "not-json",
        json.dumps(
            {
                "type": "session",
                "version": 3,
                "id": session_id,
                "cwd": str(cwd.resolve()),
                "title": header_title,
            }
        ),
        *(json.dumps(row) for row in rows),
    ]
    path.write_text("\n".join(payload) + "\n", encoding="utf-8")


def test_omp_start_cmd_uses_registry_and_exact_resume_path(tmp_path, monkeypatch):
    extension = tmp_path / "tmuxbot-session-handoff.ts"
    extension.write_text("export default () => {};\n", encoding="utf-8")
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(
        omp.ProviderDiscovery,
        "resolve_executable",
        staticmethod(lambda name: "/opt/omp/bin/omp" if name == "omp" else None),
    )
    monkeypatch.setattr(
        omp,
        "provider_launch_arguments",
        lambda name: ("--approval-mode", "yolo", "--extension", str(extension.resolve())),
    )

    base = omp._start_cmd()
    resumed = omp._start_cmd(transcript)

    assert resumed == f"{base} --resume {transcript.resolve()}"
    assert not {"--approve", "--session", "--continue", "--mode", "-p"}.intersection(
        shlex.split(resumed)
    )


def test_omp_runtime_recognizes_custom_binary_basename(monkeypatch):
    monkeypatch.setattr(
        omp.ProviderDiscovery,
        "resolve_executable",
        staticmethod(lambda name: "/opt/omp/bin/custom-omp" if name == "omp" else None),
    )

    backend = OmpBackend()

    assert backend.is_running_command("custom-omp")
    assert backend.running_command_names == frozenset({"omp", "custom-omp"})


def test_terminal_status_enrichment_uses_exact_session_file_size(tmp_path):
    cwd = tmp_path / "repo"
    cwd.mkdir()
    transcript = tmp_path / "session.jsonl"
    write_transcript(transcript, cwd, [])
    backend = OmpBackend()

    status = backend.enrich_terminal_status(
        binding(cwd, transcript),
        TerminalStatus(state=TerminalState.IDLE, model="GPT-5.6 Sol"),
    )

    assert status is not None
    assert status.session_file_size_bytes == transcript.stat().st_size
    assert "会话文件" not in backend.format_status_footer(status)


def test_runtime_metadata_derives_context_from_usage_and_model_config(tmp_path):
    agent_dir = tmp_path / "agent"
    cwd = tmp_path / "repo"
    cwd.mkdir()
    transcript = agent_dir / "sessions" / "repo" / "session.jsonl"
    write_transcript(
        transcript,
        cwd,
        [
            {
                "type": "model_change",
                "id": "model",
                "parentId": None,
                "model": "demo/gpt-status",
            },
            {
                "type": "message",
                "id": "assistant",
                "parentId": "model",
                "message": {
                    "role": "assistant",
                    "provider": "demo",
                    "model": "gpt-status",
                    "usage": {
                        "input": 1_000,
                        "output": 2_000,
                        "cacheRead": 176_900,
                        "cacheWrite": 100,
                        "totalTokens": 180_000,
                        "orchestration": {"input": 300, "output": 200},
                    },
                },
            },
        ],
    )
    (agent_dir / "models.yml").write_text(
        yaml.safe_dump(
            {"providers": {"demo": {"models": [{"id": "gpt-status", "contextWindow": 360_000}]}}}
        ),
        encoding="utf-8",
    )

    metadata = OmpBackend().current_runtime_metadata(binding(cwd, transcript))

    assert metadata.context_used == 180_000
    assert metadata.context_limit == 360_000
    assert metadata.context_percent == 50.0
    assert metadata.session_total_tokens == 3_600
    assert metadata.cache_hit_rate == pytest.approx(176_900 / 178_000)


def test_current_branch_tolerates_title_bad_lines_and_ignores_old_branch(tmp_path):
    cwd = tmp_path / "repo"
    cwd.mkdir()
    transcript = tmp_path / "session.jsonl"
    write_transcript(
        transcript,
        cwd,
        [
            {"type": "model_change", "id": "root", "parentId": None, "model": "openai/root"},
            {"type": "message", "id": "old", "parentId": "root", "message": {"role": "user"}},
            {"type": "reset_boundary", "id": "reset", "parentId": "root"},
            {"type": "message", "id": "leaf", "parentId": "reset", "message": {"role": "user"}},
            {"type": "custom", "customType": "ignored-no-id", "data": {}},
        ],
    )
    route = binding(cwd, transcript)

    assert OmpBackend().session_identity(route, transcript).session_id == "session-1"
    branch = current_jsonl_branch(transcript)
    assert [row["id"] for row in branch] == ["root", "reset", "leaf"]
    assert all(row["type"] not in {"title", "session"} for row in branch)


def test_find_active_jsonl_uses_only_exact_valid_pin(tmp_path):
    cwd = tmp_path / "repo"
    cwd.mkdir()
    transcript = tmp_path / "exact.jsonl"
    write_transcript(transcript, cwd, [])

    assert OmpBackend().find_active_jsonl(binding(cwd, transcript)) == transcript
    assert OmpBackend().find_active_jsonl(binding(tmp_path / "other", transcript)) is None
    assert OmpBackend().find_active_jsonl(binding(cwd)) is None


def test_runtime_metadata_uses_current_branch_native_v3_shapes(tmp_path):
    cwd = tmp_path / "repo"
    cwd.mkdir()
    transcript = tmp_path / "session.jsonl"
    write_transcript(
        transcript,
        cwd,
        [
            {"type": "model_change", "id": "root", "parentId": None, "model": "openai/base"},
            {
                "type": "thinking_level_change",
                "id": "think",
                "parentId": "root",
                "thinkingLevel": "high",
            },
            {"type": "title_change", "id": "title", "parentId": "think", "title": "Current title"},
            {
                "type": "message",
                "id": "assistant",
                "parentId": "title",
                "timestamp": "2026-08-14T00:00:00Z",
                "message": {
                    "role": "assistant",
                    "provider": "fallback-provider",
                    "model": "fallback-model",
                    "usage": {
                        "input": 100,
                        "output": 20,
                        "cacheRead": 50,
                        "cacheWrite": 5,
                        "cost": {"total": 0.25},
                    },
                    "content": [],
                },
            },
            {
                "type": "model_change",
                "id": "canonical",
                "parentId": "assistant",
                "model": "aisupertoken/gpt-5.6-sol",
            },
        ],
    )
    backend = OmpBackend()
    route = binding(cwd, transcript)

    metadata = backend.current_runtime_metadata(route)

    assert metadata.provider == "aisupertoken"
    assert metadata.model == "gpt-5.6-sol"
    assert metadata.effort == "high"
    assert metadata.session_name == "Current title"
    assert metadata.input_tokens == 100
    assert metadata.output_tokens == 20
    assert metadata.cache_read_tokens == 50
    assert metadata.cache_write_tokens == 5
    assert metadata.cache_hit_rate == 50 / 155
    assert metadata.cost_usd == pytest.approx(0.25)
    assert backend.aggregate_usage(transcript)["model"] == "gpt-5.6-sol"


def test_title_slot_precedes_header_and_assistant_model_is_fallback(tmp_path):
    cwd = tmp_path / "repo"
    cwd.mkdir()
    transcript = tmp_path / "session.jsonl"
    write_transcript(
        transcript,
        cwd,
        [
            {
                "type": "message",
                "id": "assistant",
                "parentId": None,
                "message": {
                    "role": "assistant",
                    "provider": "anthropic",
                    "model": "claude-sonnet",
                    "content": [],
                },
            }
        ],
        slot_title="Current slot",
        header_title="Stale header",
    )

    metadata = OmpBackend().current_runtime_metadata(binding(cwd, transcript))
    assert metadata.session_name == "Current slot"
    assert (metadata.provider, metadata.model) == ("anthropic", "claude-sonnet")


def test_event_parser_emits_tool_text_and_plan_file_update():
    row = {
        "type": "message",
        "id": "entry-1",
        "message": {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "check repository"},
                {
                    "type": "toolCall",
                    "id": "call-bash",
                    "name": "bash",
                    "arguments": {"command": "git status --short"},
                },
                {
                    "type": "toolCall",
                    "id": "call-plan",
                    "name": "write",
                    "arguments": {
                        "path": "local://native-adapter-plan.md",
                        "content": "# Ship it",
                    },
                },
                {"type": "text", "text": "Ready <now>"},
            ],
            "stopReason": "stop",
        },
    }

    events = OmpBackend().parse_event(json.dumps(row), "session-1")

    assert [event.kind for event in events] == [
        ProviderEventKind.TOOL_PROGRESS,
        ProviderEventKind.PLAN_UPDATE,
        ProviderEventKind.FINAL_TEXT,
    ]
    assert events[1].event_id.endswith(":call-plan")
    assert events[1].text == "# Ship it"
    assert events[2].text == "Ready &lt;now&gt;"


def test_successful_edit_result_projects_only_safe_structured_diffs():
    row = {
        "type": "message",
        "id": "edit-result",
        "message": {
            "role": "toolResult",
            "toolCallId": "call-edit",
            "toolName": "edit",
            "isError": False,
            "details": {
                "perFileResults": [
                    {
                        "path": "src/app.py",
                        "diff": '-1|print("<old>")\n+1|print("ready & safe")',
                    },
                    {
                        "path": ".env",
                        "diff": "-TOKEN=old\n+TOKEN=secret",
                    },
                ]
            },
        },
    }

    events = OmpBackend().parse_event(json.dumps(row), "session-1")

    assert len(events) == 1
    assert events[0].kind == ProviderEventKind.TOOL_PROGRESS
    assert "✅ <b>代码 diff</b>" in events[0].text
    assert "<code>src/app.py</code>" in events[0].text
    assert "&lt;old&gt;" in events[0].text
    assert "ready &amp; safe" in events[0].text
    assert ".env" not in events[0].text
    assert "TOKEN=secret" not in events[0].text


def test_successful_write_result_projects_cached_local_content_after_commit():
    backend = OmpBackend()
    call = {
        "type": "message",
        "id": "write-call",
        "message": {
            "role": "assistant",
            "content": [
                {
                    "type": "toolCall",
                    "id": "call-write",
                    "name": "write",
                    "arguments": {
                        "path": "src/new.py",
                        "content": 'print("<ready> & running")',
                    },
                }
            ],
        },
    }
    result = {
        "type": "message",
        "id": "write-result",
        "message": {
            "role": "toolResult",
            "toolCallId": "call-write",
            "toolName": "write",
            "isError": False,
        },
    }

    start_events = backend.parse_event(json.dumps(call), "session-1")
    result_events = backend.parse_event(json.dumps(result), "session-1")

    assert [event.kind for event in start_events] == [ProviderEventKind.TOOL_PROGRESS]
    assert len(result_events) == 1
    assert result_events[0].text == (
        "✅ <b>写入代码片段</b> <code>src/new.py</code>\n"
        "<pre>print(&quot;&lt;ready&gt; &amp; running&quot;)</pre>"
    )


def test_failed_internal_and_sensitive_writes_never_project_content():
    backend = OmpBackend()

    for call_id, path in (
        ("call-failed", "src/failed.py"),
        ("call-internal", "xd://lsp"),
        ("call-secret", "config/api-token.json"),
    ):
        backend.parse_event(
            json.dumps(
                {
                    "type": "message",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "toolCall",
                                "id": call_id,
                                "name": "write",
                                "arguments": {"path": path, "content": "DO_NOT_SEND"},
                            }
                        ],
                    },
                }
            ),
            "session-1",
        )

    failed = backend.parse_event(
        json.dumps(
            {
                "type": "message",
                "message": {
                    "role": "toolResult",
                    "toolCallId": "call-failed",
                    "toolName": "write",
                    "isError": True,
                },
            }
        ),
        "session-1",
    )
    internal = backend.parse_event(
        json.dumps(
            {
                "type": "message",
                "message": {
                    "role": "toolResult",
                    "toolCallId": "call-internal",
                    "toolName": "write",
                    "isError": False,
                },
            }
        ),
        "session-1",
    )
    secret = backend.parse_event(
        json.dumps(
            {
                "type": "message",
                "message": {
                    "role": "toolResult",
                    "toolCallId": "call-secret",
                    "toolName": "write",
                    "isError": False,
                },
            }
        ),
        "session-1",
    )

    assert len(failed) == 1
    assert failed[0].text == "⚠️ <b>写入失败</b> <code>src/failed.py</code>"
    assert failed[0].metadata["tool_call_id"] == "call-failed"
    assert failed[0].metadata["tool_phase"] == "result"
    assert internal == []
    assert secret == []


def test_write_preview_is_bounded_before_it_reaches_the_im_aggregator():
    backend = OmpBackend()
    content = "\n".join(f"line {index:02d}: " + ("x" * 80) for index in range(80))
    backend.parse_event(
        json.dumps(
            {
                "type": "message",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "toolCall",
                            "id": "call-large",
                            "name": "write",
                            "arguments": {"path": "src/large.py", "content": content},
                        }
                    ],
                },
            }
        ),
        "session-1",
    )

    events = backend.parse_event(
        json.dumps(
            {
                "type": "message",
                "message": {
                    "role": "toolResult",
                    "toolCallId": "call-large",
                    "toolName": "write",
                    "isError": False,
                },
            }
        ),
        "session-1",
    )

    assert len(events) == 1
    assert "… 已截断" in events[0].text
    assert len(events[0].text) < 1_500


def test_event_parser_ignores_xd_propose_and_removed_plan_protocols():
    backend = OmpBackend()
    write_events = backend.parse_event(
        json.dumps(
            {
                "type": "message",
                "id": "write",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "toolCall",
                            "id": "proposal",
                            "name": "write",
                            "arguments": {"path": "xd://propose", "content": "not a plan"},
                        }
                    ],
                },
            }
        )
    )
    assert [event.kind for event in write_events] == [ProviderEventKind.TOOL_PROGRESS]
    assert (
        backend.parse_event(
            json.dumps({"type": "custom_message", "customType": "proposed-plan", "content": "old"})
        )
        == []
    )
    assert (
        backend.parse_event(
            json.dumps(
                {
                    "type": "message",
                    "message": {
                        "role": "toolResult",
                        "toolName": "plan_mode_complete",
                        "details": {"plan": "old"},
                    },
                }
            )
        )
        == []
    )


def test_provider_error_is_fail_closed_and_user_cancel_is_silent():
    backend = OmpBackend()
    failed = backend.parse_event(
        json.dumps(
            {
                "type": "message",
                "id": "error",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "partial"}],
                    "stopReason": "error",
                    "errorMessage": "Request timed out",
                },
            }
        )
    )
    cancelled = backend.parse_event(
        json.dumps(
            {
                "type": "message",
                "message": {
                    "role": "assistant",
                    "content": [],
                    "stopReason": "error",
                    "errorMessage": "This operation was aborted",
                },
            }
        )
    )

    assert [event.kind for event in failed] == [ProviderEventKind.PROVIDER_ERROR]
    assert cancelled == []


def test_canonical_compaction_emits_lifecycle_and_stable_metadata(tmp_path):
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("{}\n", encoding="utf-8")
    offset = transcript.stat().st_size
    row = {
        "type": "compaction",
        "id": "compact",
        "tokensBefore": 64000,
    }
    with transcript.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(row) + "\n")

    backend = OmpBackend()
    events = backend.parse_event(json.dumps(row))
    assert events[0].kind == ProviderEventKind.LIFECYCLE_CHANGE
    assert events[0].metadata["lifecycle"] == "compaction_end"
    assert backend.compact_metadata_since(transcript, offset) == {
        "preTokens": 64000,
        "postTokens": None,
        "durationMs": None,
        "trigger": "manual",
    }


def test_legacy_compaction_shapes_do_not_count(tmp_path):
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(
        json.dumps({"type": "compact_boundary", "preTokens": 10, "postTokens": 5}) + "\n",
        encoding="utf-8",
    )
    assert OmpBackend().compact_metadata_since(transcript) is None


def test_todo_uses_latest_successful_native_phase_snapshot_on_current_branch(tmp_path):
    cwd = tmp_path / "repo"
    cwd.mkdir()
    transcript = tmp_path / "session.jsonl"
    write_transcript(
        transcript,
        cwd,
        [
            {
                "type": "message",
                "id": "todo",
                "parentId": None,
                "message": {
                    "role": "toolResult",
                    "toolName": "todo",
                    "details": {
                        "phases": [
                            {
                                "name": "Implementation",
                                "tasks": [
                                    {"content": "Audit", "status": "completed"},
                                    {"content": "Implement", "status": "in_progress"},
                                    {
                                        "content": "Wait",
                                        "status": "blocked",
                                        "blocker": "Need approval",
                                    },
                                    {"content": "Dropped", "status": "abandoned"},
                                ],
                            }
                        ]
                    },
                    "isError": False,
                },
            },
            {
                "type": "custom",
                "id": "manual",
                "parentId": "todo",
                "customType": "user_todo_edit",
                "data": {
                    "phases": [
                        {
                            "name": "Verification",
                            "tasks": [{"content": "Smoke", "status": "pending"}],
                        }
                    ]
                },
            },
        ],
    )

    assert OmpBackend().read_tasks(binding(cwd, transcript)) == [
        {"phase": "Verification", "content": "Smoke", "status": "pending"}
    ]


def test_invalid_latest_todo_snapshot_fails_closed(tmp_path):
    cwd = tmp_path / "repo"
    cwd.mkdir()
    transcript = tmp_path / "session.jsonl"
    write_transcript(
        transcript,
        cwd,
        [
            {
                "type": "custom",
                "id": "bad",
                "parentId": None,
                "customType": "user_todo_edit",
                "data": {
                    "phases": [{"name": "Bad", "tasks": [{"content": "x", "status": "deleted"}]}]
                },
            }
        ],
    )
    assert OmpBackend().read_tasks(binding(cwd, transcript)) == []


def test_failed_todo_result_does_not_replace_last_successful_snapshot(tmp_path):
    cwd = tmp_path / "repo"
    cwd.mkdir()
    transcript = tmp_path / "session.jsonl"
    write_transcript(
        transcript,
        cwd,
        [
            {
                "type": "message",
                "id": "good",
                "parentId": None,
                "message": {
                    "role": "toolResult",
                    "toolName": "todo",
                    "details": {
                        "phases": [
                            {"name": "Work", "tasks": [{"content": "Keep", "status": "pending"}]}
                        ]
                    },
                    "isError": False,
                },
            },
            {
                "type": "message",
                "id": "failed",
                "parentId": "good",
                "message": {
                    "role": "toolResult",
                    "toolName": "todo",
                    "details": {"phases": "broken"},
                    "isError": True,
                },
            },
        ],
    )
    assert OmpBackend().read_tasks(binding(cwd, transcript)) == [
        {"phase": "Work", "content": "Keep", "status": "pending"}
    ]


def test_plan_mode_uses_last_mode_and_current_branch_plan_file(tmp_path):
    cwd = tmp_path / "repo"
    cwd.mkdir()
    transcript = tmp_path / "session.jsonl"
    write_transcript(
        transcript,
        cwd,
        [
            {"type": "mode_change", "id": "mode", "parentId": None, "mode": "plan"},
            {
                "type": "message",
                "id": "plan",
                "parentId": "mode",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "toolCall",
                            "id": "write-plan",
                            "name": "write",
                            "arguments": {
                                "path": "local://ship-plan.md",
                                "content": "# Current plan",
                            },
                        }
                    ],
                },
            },
        ],
    )
    backend = OmpBackend()
    route = binding(cwd, transcript)

    snapshot = backend.read_plan_mode(route)
    assert snapshot is not None
    assert snapshot.status == "active"
    assert snapshot.footer == "📝 plan active"
    assert snapshot.plan == "# Current plan"

    with transcript.open("a", encoding="utf-8") as stream:
        stream.write(
            json.dumps({"type": "mode_change", "id": "off", "parentId": "plan", "mode": "default"})
            + "\n"
        )
    assert backend.read_plan_mode(route) is None


def test_command_options_match_native_omp_control_semantics():
    opts = OmpBackend().command_opts()

    assert set(opts) == {"/new", "/fork", "/compact", "/clear", "/fresh"}
    assert opts["/new"].expect_new_session is True
    assert opts["/new"].defer_new_session_persistence is True
    assert opts["/fork"].expect_new_session is True
    assert opts["/fork"].expect_session_handoff is True
    assert opts["/compact"].expect_compact_done is True
    assert opts["/compact"].expect_session_handoff is True
    assert opts["/clear"].expect_new_session is False
    assert opts["/fresh"].expect_new_session is False
    assert OmpBackend().interactive_session_handoff_commands() == frozenset({"/resume", "/import"})
