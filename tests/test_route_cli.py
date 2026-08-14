import json
from pathlib import Path

import pytest
import yaml

from tmuxbot.__main__ import build_parser
from tmuxbot.route_cli import RouteStore, run_route_command
from tmuxbot.validation import ConfigValidationError


def route(name: str = "alpha", **overrides) -> dict[str, object]:
    item = {
        "name": name,
        "channel": "telegram",
        "bot_token_env": "TG_BOT_TOKEN",
        "chat_id": -1001,
        "thread_id": 42,
        "tmux_session": name,
        "tmux_window": 0,
        "tmux_pane": 0,
        "cwd": f"/tmp/{name}",
        "backend": "omp",
    }
    item.update(overrides)
    return item


def write_routes(path: Path, entries: list[dict[str, object]]) -> None:
    path.write_text(
        yaml.safe_dump({"bindings": entries}, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def test_route_store_round_trips_feishu_thread_root_anchor(tmp_path, capsys):
    path = tmp_path / "bindings.yaml"
    write_routes(
        path,
        [
            route(
                channel="feishu",
                bot_token_env="FEISHU",
                chat_id="oc_group",
                thread_id="omt_topic",
                thread_root_message_id="om_root",
            )
        ],
    )

    item = RouteStore(path).inspect("alpha")

    assert item.thread_root_message_id == "om_root"
    assert run_route_command(["--file", str(path), "inspect", "alpha", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["thread_root_message_id"] == "om_root"


def test_route_store_ignores_retired_idle_timeout_and_keeps_mention_policy(tmp_path):
    path = tmp_path / "bindings.yaml"
    write_routes(
        path,
        [route(mention_required=False, cli_idle_timeout_seconds=0)],
    )

    item = RouteStore(path).inspect("alpha")

    assert item.mention_required is False
    assert "cli_idle_timeout_seconds" not in item.__dict__


def test_route_store_lists_and_inspects_exact_routes(tmp_path):
    path = tmp_path / "bindings.yaml"
    write_routes(path, [route(), route("beta", thread_id=43, backend="codex")])
    store = RouteStore(path)

    assert [item.name for item in store.list()] == ["alpha", "beta"]
    assert store.inspect("beta").backend == "codex"


def test_route_store_bind_validates_full_candidate_before_atomic_replace(tmp_path):
    path = tmp_path / "bindings.yaml"
    write_routes(path, [route()])
    original = path.read_text(encoding="utf-8")
    store = RouteStore(path)

    with pytest.raises(ConfigValidationError, match="duplicate source"):
        store.bind(route("duplicate", tmux_session="other", cwd="/tmp/other"))

    assert path.read_text(encoding="utf-8") == original
    assert not list(tmp_path.glob(".bindings.yaml.*.tmp"))


def test_route_store_refuses_to_replace_symlinked_bindings_file(tmp_path):
    real = tmp_path / "real.yaml"
    link = tmp_path / "bindings.yaml"
    write_routes(real, [route()])
    link.symlink_to(real)

    with pytest.raises(ConfigValidationError, match="symbolic link"):
        RouteStore(link).bind(route("beta", thread_id=43))


def test_route_store_bind_and_unbind_persist_valid_yaml(tmp_path):
    path = tmp_path / "bindings.yaml"
    write_routes(path, [route()])
    store = RouteStore(path)

    store.bind(route("beta", thread_id=43, backend="codex"))
    assert [item.name for item in store.list()] == ["alpha", "beta"]

    removed = store.unbind("alpha")
    assert removed.name == "alpha"
    assert [item.name for item in store.list()] == ["beta"]


def test_route_cli_bind_accepts_explicit_mention_policy(tmp_path, capsys):
    path = tmp_path / "bindings.yaml"
    write_routes(path, [])

    exit_code = run_route_command(
        [
            "--file",
            str(path),
            "bind",
            "--name",
            "alpha",
            "--channel",
            "telegram",
            "--credential",
            "TG_BOT_TOKEN",
            "--chat-id",
            "-1001",
            "--thread-id",
            "42",
            "--tmux-session",
            "alpha",
            "--cwd",
            "/tmp/alpha",
            "--backend",
            "omp",
            "--no-mention-required",
        ]
    )

    assert exit_code == 0
    assert capsys.readouterr().out.strip() == "bound: alpha"
    stored = yaml.safe_load(path.read_text(encoding="utf-8"))["bindings"][0]
    assert stored["mention_required"] is False
    assert "cli_idle_timeout_seconds" not in stored


def test_route_cli_list_json_is_machine_readable(tmp_path, capsys):
    path = tmp_path / "bindings.yaml"
    write_routes(path, [route()])

    exit_code = run_route_command(["--file", str(path), "list", "--json"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload[0]["name"] == "alpha"
    assert payload[0]["thread_id"] == 42
    assert payload[0]["backend"] == "omp"


def test_main_parser_exposes_route_namespace_without_consuming_route_flags(tmp_path):
    path = tmp_path / "bindings.yaml"
    args = build_parser().parse_args(["route", "--file", str(path), "list", "--json"])

    assert args.command == "route"
    assert args.route_file == path
    assert args.route_args == ["list", "--json"]


def test_route_cli_validate_reports_success(tmp_path, capsys):
    path = tmp_path / "bindings.yaml"
    write_routes(path, [route()])

    assert run_route_command(["--file", str(path), "validate"]) == 0
    assert "1 route" in capsys.readouterr().out
