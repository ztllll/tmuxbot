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
    TmuxInventory,
    TmuxInventoryError,
    classify_inventory,
)
from tmuxbot.state import Binding


def test_tmux_inventory_classifies_exact_fields_without_mutation():
    panes = [
        TmuxPaneRecord("alpha:0.1", "alpha", 0, 1, "python", "/repo", 4321),
        TmuxPaneRecord("other:2.0", "other", 2, 0, "bash", "/tmp", 99),
    ]
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


class FakeRunner:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, command, **kwargs):
        self.calls.append((command, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def completed(returncode=0, stdout=b"", stderr=b""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def one_pane_responses(*, cwd=b"/repo", command=b"python"):
    return [
        completed(stdout=b"%7\n"),
        completed(stdout=b"alpha\n"),
        completed(stdout=b"0\n"),
        completed(stdout=b"1\n"),
        completed(stdout=command + b"\n"),
        completed(stdout=cwd + b"\n"),
        completed(stdout=b"4321\n"),
    ]


def test_tmux_inventory_uses_only_deadline_bound_read_only_byte_commands(monkeypatch):
    runner = FakeRunner(one_pane_responses())
    clock_values = iter([10.0, 10.0, 10.2, 10.4, 10.6, 10.8, 11.0, 11.2])
    monkeypatch.setattr("tmuxbot.control_plane.tmux_inventory.subprocess.run", runner)
    monkeypatch.setattr(
        "tmuxbot.control_plane.tmux_inventory.time.monotonic",
        lambda: next(clock_values),
    )

    panes = TmuxInventory().list_panes()

    assert panes == [
        TmuxPaneRecord("alpha:0.1", "alpha", 0, 1, "python", "/repo", 4321)
    ]
    assert [call[0] for call in runner.calls] == [
        ["tmux", "list-panes", "-a", "-F", "#{pane_id}"],
        ["tmux", "display-message", "-t", "%7", "-p", "#{session_name}"],
        ["tmux", "display-message", "-t", "%7", "-p", "#{window_index}"],
        ["tmux", "display-message", "-t", "%7", "-p", "#{pane_index}"],
        [
            "tmux",
            "display-message",
            "-t",
            "%7",
            "-p",
            "#{pane_current_command}",
        ],
        [
            "tmux",
            "display-message",
            "-t",
            "%7",
            "-p",
            "#{pane_current_path}",
        ],
        ["tmux", "display-message", "-t", "%7", "-p", "#{pane_pid}"],
    ]
    assert [call[1]["timeout"] for call in runner.calls] == pytest.approx(
        [3.0, 2.8, 2.6, 2.4, 2.2, 2.0, 1.8]
    )
    assert [
        {key: value for key, value in call[1].items() if key != "timeout"}
        for call in runner.calls
    ] == [
        {"capture_output": True, "text": False, "check": False}
        for _ in runner.calls
    ]


def test_tmux_inventory_uses_configured_total_timeout(monkeypatch):
    runner = FakeRunner(
        [
            completed(
                returncode=1,
                stderr=b"no server running on /tmp/tmux-1000/default\n",
            )
        ]
    )
    clock_values = iter([5.0, 5.0])
    monkeypatch.setattr("tmuxbot.control_plane.tmux_inventory.subprocess.run", runner)
    monkeypatch.setattr(
        "tmuxbot.control_plane.tmux_inventory.time.monotonic",
        lambda: next(clock_values),
    )

    assert TmuxInventory(timeout_seconds=1.25).list_panes() == []
    assert runner.calls[0][1]["timeout"] == 1.25


def test_tmux_inventory_preserves_text_framing_and_replaces_invalid_bytes(
    monkeypatch,
):
    raw_cwd = b"/repo\nwith\told-separator-\x1f-and-byte-\xff"
    runner = FakeRunner(one_pane_responses(cwd=raw_cwd, command=b"python\tworker"))
    monkeypatch.setattr("tmuxbot.control_plane.tmux_inventory.subprocess.run", runner)

    panes = TmuxInventory().list_panes()

    assert len(panes) == 1
    assert panes[0].command == "python\tworker"
    assert panes[0].cwd == "/repo\nwith\told-separator-\x1f-and-byte-\ufffd"
    assert not any(0xD800 <= ord(character) <= 0xDFFF for character in panes[0].cwd)
    panes[0].cwd.encode("utf-8")


def test_tmux_inventory_requires_ascii_numeric_fields(monkeypatch):
    responses = one_pane_responses()
    responses[2] = completed(stdout=b"\xff\n")
    runner = FakeRunner(responses)
    monkeypatch.setattr("tmuxbot.control_plane.tmux_inventory.subprocess.run", runner)

    with pytest.raises(TmuxInventoryError) as raised:
        TmuxInventory().list_panes()

    assert raised.value.code == "command_failed"
    assert raised.value.detail == "tmux inventory output was malformed"


def test_tmux_inventory_enforces_one_total_deadline(monkeypatch):
    runner = FakeRunner(one_pane_responses())
    clock_values = iter([0.0, 0.0, 1.0, 2.0, 3.1])
    monkeypatch.setattr("tmuxbot.control_plane.tmux_inventory.subprocess.run", runner)
    monkeypatch.setattr(
        "tmuxbot.control_plane.tmux_inventory.time.monotonic",
        lambda: next(clock_values),
    )

    with pytest.raises(TmuxInventoryError) as raised:
        TmuxInventory(timeout_seconds=3.0).list_panes()

    assert raised.value.code == "timeout"
    assert raised.value.detail == "tmux inventory command timed out"
    assert len(runner.calls) == 3


def test_tmux_inventory_returns_empty_for_exact_standard_no_server(monkeypatch):
    runner = FakeRunner(
        [
            completed(
                returncode=1,
                stderr=b"no server running on /tmp/tmux-1000/default\n",
            )
        ]
    )
    monkeypatch.setattr("tmuxbot.control_plane.tmux_inventory.subprocess.run", runner)

    assert TmuxInventory().list_panes() == []


@pytest.mark.parametrize(
    "stderr",
    [
        b"prefix: no server running on /tmp/tmux-1000/default\n",
        b"no server running on /tmp/tmux-1000/default: socket error\n",
        b"no server running on /tmp/tmux-1000/default extra text\n",
        b"NO SERVER RUNNING ON /tmp/tmux-1000/default\n",
        b"no server running on \n",
        b"can't find server\n",
        b"no tmux server found\n",
        b"no server running on /tmp/tmux.sock\npermission denied\n",
        b"no server running on /tmp/tmux.sock\nauthentication failed\n",
    ],
)
def test_tmux_inventory_rejects_similar_but_nonstandard_no_server_text(
    monkeypatch, stderr
):
    runner = FakeRunner([completed(returncode=1, stderr=stderr)])
    monkeypatch.setattr("tmuxbot.control_plane.tmux_inventory.subprocess.run", runner)

    with pytest.raises(TmuxInventoryError) as raised:
        TmuxInventory().list_panes()

    assert raised.value.code in {"permission", "command_failed"}


def test_tmux_inventory_rejects_malformed_pane_ids(monkeypatch):
    runner = FakeRunner([completed(stdout=b"%7\nnot-a-pane\n")])
    monkeypatch.setattr("tmuxbot.control_plane.tmux_inventory.subprocess.run", runner)

    with pytest.raises(TmuxInventoryError) as raised:
        TmuxInventory().list_panes()

    assert raised.value.code == "command_failed"
    assert raised.value.detail == "tmux inventory output was malformed"


def test_tmux_inventory_reports_pane_disappearing_during_field_read(monkeypatch):
    runner = FakeRunner(
        [
            completed(stdout=b"%7\n"),
            completed(returncode=1, stderr=b"can't find pane: %7\n"),
        ]
    )
    monkeypatch.setattr("tmuxbot.control_plane.tmux_inventory.subprocess.run", runner)

    with pytest.raises(TmuxInventoryError) as raised:
        TmuxInventory().list_panes()

    assert raised.value.code == "changed"
    assert raised.value.detail == "tmux pane changed during inventory"


def test_tmux_inventory_reports_missing_binary_without_leaking_path(monkeypatch):
    runner = FakeRunner([FileNotFoundError(2, "not found", "/secret/bin/tmux")])
    monkeypatch.setattr("tmuxbot.control_plane.tmux_inventory.subprocess.run", runner)

    with pytest.raises(TmuxInventoryError) as raised:
        TmuxInventory().list_panes()

    assert raised.value.code == "unavailable"
    assert raised.value.detail == "tmux executable is unavailable"
    assert "/secret" not in str(raised.value)


def test_tmux_inventory_reports_timeout_without_leaking_command_output(monkeypatch):
    runner = FakeRunner(
        [
            subprocess.TimeoutExpired(
                cmd=["tmux", "list-panes"],
                timeout=0.25,
                stderr=b"/secret/socket",
            )
        ]
    )
    monkeypatch.setattr("tmuxbot.control_plane.tmux_inventory.subprocess.run", runner)

    with pytest.raises(TmuxInventoryError) as raised:
        TmuxInventory(timeout_seconds=0.25).list_panes()

    assert raised.value.code == "timeout"
    assert raised.value.detail == "tmux inventory command timed out"
    assert "/secret" not in str(raised.value)


@pytest.mark.parametrize(
    ("failure", "expected_code", "expected_detail"),
    [
        (
            completed(
                returncode=1,
                stderr=b"no server running on /secret/tmux.sock: Permission denied\n",
            ),
            "permission",
            "tmux inventory access was denied",
        ),
        (
            completed(
                returncode=2, stderr=b"unexpected /secret/socket failure\n"
            ),
            "command_failed",
            "tmux inventory command failed",
        ),
    ],
)
def test_tmux_inventory_sanitizes_nonzero_command_errors(
    monkeypatch, failure, expected_code, expected_detail
):
    runner = FakeRunner([failure])
    monkeypatch.setattr("tmuxbot.control_plane.tmux_inventory.subprocess.run", runner)

    with pytest.raises(TmuxInventoryError) as raised:
        TmuxInventory().list_panes()

    assert raised.value.code == expected_code
    assert raised.value.detail == expected_detail
    assert "/secret" not in str(raised.value)


@pytest.mark.parametrize(
    ("failure", "expected_code", "expected_detail"),
    [
        (
            PermissionError(13, "denied", "/secret/bin/tmux"),
            "permission",
            "tmux inventory access was denied",
        ),
        (
            OSError(5, "I/O error", "/secret/bin/tmux"),
            "unavailable",
            "tmux inventory command is unavailable",
        ),
    ],
)
def test_tmux_inventory_sanitizes_process_launch_errors(
    monkeypatch, failure, expected_code, expected_detail
):
    runner = FakeRunner([failure])
    monkeypatch.setattr("tmuxbot.control_plane.tmux_inventory.subprocess.run", runner)

    with pytest.raises(TmuxInventoryError) as raised:
        TmuxInventory().list_panes()

    assert raised.value.code == expected_code
    assert raised.value.detail == expected_detail
    assert "/secret" not in str(raised.value)
