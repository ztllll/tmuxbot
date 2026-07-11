from pathlib import Path
from types import SimpleNamespace

import pytest

from tmuxbot.control_plane.models import (
    SessionClass,
    SessionInventoryItem,
    TmuxPaneRecord,
)
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
