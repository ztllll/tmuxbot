import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from tmuxbot.control_plane.models import (
    SessionClass,
    SessionInventoryItem,
    TmuxPaneRecord,
)
from tmuxbot.control_plane.tmux_inventory import (
    TMUX_FIELD_SEPARATOR,
    TMUX_FORMAT,
    TmuxInventory,
    TmuxInventoryError,
    classify_inventory,
    parse_tmux_rows,
)
from tmuxbot.state import Binding


def test_tmux_inventory_parses_exact_fields_and_classifies_without_mutation():
    separator = TMUX_FIELD_SEPARATOR
    rows = (
        f"alpha{separator}0{separator}1{separator}python{separator}/repo{separator}4321\n"
        f"other{separator}2{separator}0{separator}bash{separator}/tmp{separator}99\n"
    )
    panes = parse_tmux_rows(rows)
    binding = Binding(
        name="codex-main",
        chat_id=1,
        thread_id=None,
        tmux_session="alpha",
        tmux_window=0,
        tmux_pane=1,
        cwd=Path("/repo"),
        backend="codex",
    )

    items = classify_inventory(panes, [binding], ignored_targets={"other:2.0"})

    assert items[0].classification == SessionClass.MANAGED
    assert items[0].binding_name == "codex-main"
    assert items[0].provider == "codex"
    assert items[1].classification == SessionClass.IGNORED
    assert panes == [
        TmuxPaneRecord(
            target="alpha:0.1",
            session_name="alpha",
            window_index=0,
            pane_index=1,
            command="python",
            cwd="/repo",
            pid=4321,
        ),
        TmuxPaneRecord(
            target="other:2.0",
            session_name="other",
            window_index=2,
            pane_index=0,
            command="bash",
            cwd="/tmp",
            pid=99,
        ),
    ]


def test_classify_inventory_marks_unbound_unignored_pane_as_orphan():
    pane = TmuxPaneRecord(
        target="loose:1.2",
        session_name="loose",
        window_index=1,
        pane_index=2,
        command="bash",
        cwd="/tmp",
        pid=99,
    )

    [item] = classify_inventory([pane], [], ignored_targets=set())

    assert item.classification == SessionClass.ORPHAN
    assert item.binding_name is None
    assert item.provider is None


def test_classify_inventory_prefers_managed_when_target_is_also_ignored():
    pane = TmuxPaneRecord("alpha:0.1", "alpha", 0, 1, "python", "/repo", 4321)
    binding = Binding("main", 1, None, "alpha", 0, 1, Path("/repo"), backend="codex")

    [item] = classify_inventory([pane], [binding], ignored_targets={"alpha:0.1"})

    assert item.classification == SessionClass.MANAGED
    assert item.binding_name == "main"
    assert item.provider == "codex"


def test_classify_inventory_does_not_mutate_any_binding_field():
    binding = Binding(
        name="codex-main",
        chat_id="oc_chat",
        thread_id=42,
        tmux_session="alpha",
        tmux_window=3,
        tmux_pane=2,
        cwd=Path("/repo"),
        backend="codex",
        bot_token_env="TG_CODEX_BOT_TOKEN",
        channel="feishu",
        mention_required=True,
        provider_session_id="provider-session",
        transcript_path=Path("/repo/transcript.jsonl"),
        last_session_id="legacy-session",
    )
    before = vars(binding).copy()
    pane = TmuxPaneRecord("alpha:3.2", "alpha", 3, 2, "python", "/repo", 4321)

    classify_inventory([pane], [binding], ignored_targets=set())

    assert vars(binding) == before


def test_session_inventory_metadata_is_defensively_copied_and_read_only():
    metadata = {"source": "tmux"}
    pane = TmuxPaneRecord("alpha:0.1", "alpha", 0, 1, "python", "/repo", 4321)
    item = SessionInventoryItem(pane, SessionClass.MANAGED, metadata=metadata)

    metadata["source"] = "changed"

    assert item.metadata == {"source": "tmux"}
    with pytest.raises(TypeError):
        item.metadata["source"] = "changed"  # type: ignore[index]


@pytest.mark.parametrize(
    ("timeout_seconds", "expected_timeout"), [(None, 3.0), (1.25, 1.25)]
)
def test_tmux_inventory_uses_only_read_only_list_panes_command_with_timeout(
    monkeypatch, timeout_seconds, expected_timeout
):
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(
            returncode=0,
            stdout=TMUX_FIELD_SEPARATOR.join(
                ["alpha", "0", "1", "python", "/repo", "4321"]
            )
            + "\n",
            stderr="",
        )

    monkeypatch.setattr("tmuxbot.control_plane.tmux_inventory.subprocess.run", fake_run)

    inventory = (
        TmuxInventory()
        if timeout_seconds is None
        else TmuxInventory(timeout_seconds=timeout_seconds)
    )
    panes = inventory.list_panes()

    assert panes[0].target == "alpha:0.1"
    assert calls == [
        (
            ["tmux", "list-panes", "-a", "-F", TMUX_FORMAT],
            {
                "capture_output": True,
                "text": True,
                "check": False,
                "timeout": expected_timeout,
            },
        )
    ]


@pytest.mark.parametrize(
    "stderr",
    [
        "no server running on /tmp/tmux-1000/default",
        "can't find server",
        "no tmux server found",
    ],
)
def test_tmux_inventory_returns_empty_only_for_explicit_no_server(monkeypatch, stderr):
    monkeypatch.setattr(
        "tmuxbot.control_plane.tmux_inventory.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=1, stdout="", stderr=stderr
        ),
    )

    assert TmuxInventory().list_panes() == []


def test_tmux_inventory_reports_missing_binary_without_leaking_path(monkeypatch):
    def missing_binary(*args, **kwargs):
        raise FileNotFoundError(2, "not found", "/secret/bin/tmux")

    monkeypatch.setattr(
        "tmuxbot.control_plane.tmux_inventory.subprocess.run", missing_binary
    )

    with pytest.raises(TmuxInventoryError) as raised:
        TmuxInventory().list_panes()

    assert raised.value.code == "unavailable"
    assert raised.value.detail == "tmux executable is unavailable"
    assert "/secret" not in str(raised.value)


def test_tmux_inventory_reports_timeout_without_leaking_command_output(monkeypatch):
    def times_out(*args, **kwargs):
        raise subprocess.TimeoutExpired(
            cmd=["tmux", "list-panes"], timeout=0.25, stderr="/secret/socket"
        )

    monkeypatch.setattr("tmuxbot.control_plane.tmux_inventory.subprocess.run", times_out)

    with pytest.raises(TmuxInventoryError) as raised:
        TmuxInventory(timeout_seconds=0.25).list_panes()

    assert raised.value.code == "timeout"
    assert raised.value.detail == "tmux inventory command timed out"
    assert "/secret" not in str(raised.value)


@pytest.mark.parametrize(
    ("failure", "expected_code", "expected_detail"),
    [
        (
            SimpleNamespace(
                returncode=1,
                stdout="",
                stderr=(
                    "no server running on /secret/tmux.sock: Permission denied"
                ),
            ),
            "permission",
            "tmux inventory access was denied",
        ),
        (
            SimpleNamespace(
                returncode=2, stdout="", stderr="unexpected /secret/socket failure"
            ),
            "command_failed",
            "tmux inventory command failed with exit status 2",
        ),
    ],
)
def test_tmux_inventory_sanitizes_nonzero_command_errors(
    monkeypatch, failure, expected_code, expected_detail
):
    monkeypatch.setattr(
        "tmuxbot.control_plane.tmux_inventory.subprocess.run",
        lambda *args, **kwargs: failure,
    )

    with pytest.raises(TmuxInventoryError) as raised:
        TmuxInventory().list_panes()

    assert raised.value.code == expected_code
    assert raised.value.detail == expected_detail
    assert "/secret" not in str(raised.value)


def test_tmux_inventory_reports_permission_error_from_process_launch(monkeypatch):
    def denied(*args, **kwargs):
        raise PermissionError(13, "denied", "/secret/bin/tmux")

    monkeypatch.setattr("tmuxbot.control_plane.tmux_inventory.subprocess.run", denied)

    with pytest.raises(TmuxInventoryError) as raised:
        TmuxInventory().list_panes()

    assert raised.value.code == "permission"
    assert raised.value.detail == "tmux inventory access was denied"
    assert "/secret" not in str(raised.value)


def test_tmux_inventory_parses_cwd_containing_tab():
    separator = TMUX_FIELD_SEPARATOR

    panes = parse_tmux_rows(
        separator.join(["alpha", "0", "1", "python", "/repo\twork", "4321"])
    )

    assert panes[0].cwd == "/repo\twork"


def test_tmux_inventory_rejects_embedded_newline_as_sanitized_command_failure(
    monkeypatch,
):
    separator = TMUX_FIELD_SEPARATOR
    output = separator.join(
        ["alpha", "0", "1", "python", "/repo\nwork", "4321"]
    )
    monkeypatch.setattr(
        "tmuxbot.control_plane.tmux_inventory.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0, stdout=output, stderr=""
        ),
    )

    with pytest.raises(TmuxInventoryError) as raised:
        TmuxInventory().list_panes()

    assert raised.value.code == "command_failed"
    assert raised.value.detail == "tmux inventory output was malformed"


@pytest.mark.parametrize(
    "row",
    [
        TMUX_FIELD_SEPARATOR.join(["alpha", "0", "1", "python", "/repo"]),
        TMUX_FIELD_SEPARATOR.join(
            ["alpha", "window", "1", "python", "/repo", "4321"]
        ),
        TMUX_FIELD_SEPARATOR.join(
            ["alpha", "0", "pane", "python", "/repo", "4321"]
        ),
        TMUX_FIELD_SEPARATOR.join(
            ["alpha", "0", "1", "python", "/repo", "pid"]
        ),
    ],
)
def test_parse_tmux_rows_rejects_malformed_rows(row):
    with pytest.raises(ValueError, match="malformed tmux row 1"):
        parse_tmux_rows(row)
