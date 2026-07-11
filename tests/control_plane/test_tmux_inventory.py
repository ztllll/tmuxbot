from pathlib import Path
from types import SimpleNamespace

import pytest

from tmuxbot.control_plane.models import SessionClass, TmuxPaneRecord
from tmuxbot.control_plane.tmux_inventory import (
    TMUX_FORMAT,
    TmuxInventory,
    classify_inventory,
    parse_tmux_rows,
)
from tmuxbot.state import Binding


def test_tmux_inventory_parses_exact_fields_and_classifies_without_mutation():
    rows = "alpha\t0\t1\tpython\t/repo\t4321\nother\t2\t0\tbash\t/tmp\t99\n"
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


def test_tmux_inventory_uses_only_read_only_list_panes_command(monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(
            returncode=0,
            stdout="alpha\t0\t1\tpython\t/repo\t4321\n",
        )

    monkeypatch.setattr("tmuxbot.control_plane.tmux_inventory.subprocess.run", fake_run)

    panes = TmuxInventory().list_panes()

    assert panes[0].target == "alpha:0.1"
    assert calls == [
        (
            ["tmux", "list-panes", "-a", "-F", TMUX_FORMAT],
            {"capture_output": True, "text": True, "check": False},
        )
    ]


def test_tmux_inventory_returns_empty_when_no_server_is_running(monkeypatch):
    monkeypatch.setattr(
        "tmuxbot.control_plane.tmux_inventory.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(returncode=1, stdout=""),
    )

    assert TmuxInventory().list_panes() == []


@pytest.mark.parametrize(
    "row",
    [
        "alpha\t0\t1\tpython\t/repo",
        "alpha\twindow\t1\tpython\t/repo\t4321",
        "alpha\t0\tpane\tpython\t/repo\t4321",
        "alpha\t0\t1\tpython\t/repo\tpid",
    ],
)
def test_parse_tmux_rows_rejects_malformed_rows(row):
    with pytest.raises(ValueError, match="malformed tmux row 1"):
        parse_tmux_rows(row)
