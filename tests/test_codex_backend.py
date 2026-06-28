import json
from pathlib import Path

from tmuxbot.backends.codex import CodexBackend
from tmuxbot.state import Binding


def _write_rollout(path: Path, cwd: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"type": "session_meta", "payload": {"cwd": cwd}}) + "\n",
        encoding="utf-8",
    )


def _binding(tmp_path: Path) -> Binding:
    return Binding(
        name="project",
        chat_id=1,
        thread_id=None,
        tmux_session="project",
        tmux_window=0,
        tmux_pane=0,
        cwd=tmp_path / "project",
        backend="codex",
        bot_token_env="TG_CODEX_BOT_TOKEN",
    )


def test_codex_find_active_jsonl_matches_binding_cwd(tmp_path, monkeypatch):
    sessions = tmp_path / "sessions"
    project = tmp_path / "project"
    project.mkdir()
    other = tmp_path / "other"
    other.mkdir()

    matching = sessions / "2026" / "06" / "19" / "rollout-project.jsonl"
    newest_other = sessions / "2026" / "06" / "19" / "rollout-other.jsonl"
    _write_rollout(matching, str(project))
    _write_rollout(newest_other, str(other))
    matching.touch()
    newest_other.touch()
    monkeypatch.setattr("tmuxbot.backends.codex.CODEX_SESSIONS_DIR", sessions)

    assert CodexBackend().find_active_jsonl(_binding(tmp_path)) == matching


def test_codex_find_active_jsonl_does_not_fallback_to_global_latest(tmp_path, monkeypatch):
    sessions = tmp_path / "sessions"
    project = tmp_path / "project"
    project.mkdir()
    other = tmp_path / "other"
    other.mkdir()

    newest_other = sessions / "2026" / "06" / "19" / "rollout-other.jsonl"
    _write_rollout(newest_other, str(other))
    monkeypatch.setattr("tmuxbot.backends.codex.CODEX_SESSIONS_DIR", sessions)

    assert CodexBackend().find_active_jsonl(_binding(tmp_path)) is None


def test_codex_update_plan_function_call_forwards_full_plan():
    line = json.dumps(
        {
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "update_plan",
                "arguments": json.dumps(
                    {
                        "explanation": "先复现，再修复。",
                        "plan": [
                            {"step": "复现 TG/飞书漏消息", "status": "completed"},
                            {"step": "补 Codex 计划解析", "status": "in_progress"},
                            {"step": "部署验证", "status": "pending"},
                        ],
                    },
                    ensure_ascii=False,
                ),
            },
        },
        ensure_ascii=False,
    )

    events = CodexBackend().parse_event(line)

    assert len(events) == 1
    kind, body = events[0]
    assert kind == "assistant_plan"
    assert "先复现，再修复。" in body
    assert "复现 TG/飞书漏消息" in body
    assert "补 Codex 计划解析" in body
    assert "部署验证" in body
    assert "completed" in body
    assert "in_progress" in body
    assert "pending" in body


def test_codex_custom_apply_patch_call_is_forwarded():
    line = json.dumps(
        {
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call",
                "name": "apply_patch",
                "input": "*** Begin Patch\n*** Update File: app.py\n@@\n-x\n+y\n*** End Patch",
            },
        },
        ensure_ascii=False,
    )

    events = CodexBackend().parse_event(line)

    assert events == [("assistant_tools", "✂️ 改文件 <code>app.py</code>")]


def test_codex_patch_apply_end_event_is_forwarded():
    line = json.dumps(
        {
            "type": "event_msg",
            "payload": {
                "type": "patch_apply_end",
                "success": True,
                "stdout": "Success. Updated the following files:\nM app.py\n",
            },
        },
        ensure_ascii=False,
    )

    events = CodexBackend().parse_event(line)

    assert events == [("assistant_tools", "✓ 改文件成功 <code>app.py</code>")]
