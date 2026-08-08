from __future__ import annotations

import json
from pathlib import Path

import yaml

from tmuxbot.__main__ import build_parser
from tmuxbot.admin_cli import (
    AdminRuntime,
    discover_feishu_topics,
    parse_telegram_topic_link,
    run_admin_command,
)
from tmuxbot.route_cli import RouteStore


def route(name: str = "alpha", **overrides) -> dict[str, object]:
    item: dict[str, object] = {
        "name": name,
        "channel": "feishu",
        "bot_token_env": "FEISHU_CODEX",
        "chat_id": "oc_old",
        "thread_id": "omt_old",
        "tmux_session": name,
        "tmux_window": 0,
        "tmux_pane": 0,
        "cwd": f"/tmp/{name}",
        "backend": "pi",
        "mention_required": False,
        "provider_session_id": "session-old",
        "transcript_path": f"/tmp/{name}.jsonl",
    }
    item.update(overrides)
    return item


def write_routes(path: Path, entries: list[dict[str, object]]) -> None:
    path.write_text(
        yaml.safe_dump({"bindings": entries}, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


class FakeRuntime(AdminRuntime):
    def __init__(
        self,
        targets: dict[str, dict[str, object]] | None = None,
        *,
        restart_error: bool = False,
    ) -> None:
        self.targets = targets or {}
        self.restart_error = restart_error
        self.restarts: list[str] = []
        self.created: list[tuple[str, Path]] = []
        self.removed: list[str] = []

    def target_status(self, target):
        return self.targets.get(target.value, {"state": "stopped", "target": target.value})

    def list_targets(self):
        return list(self.targets.values())

    def create_target(self, target, cwd):
        self.created.append((target.value, cwd))
        self.targets[target.value] = {
            "state": "running",
            "target": target.value,
            "cwd": str(cwd),
            "command": "bash",
            "dead": False,
        }

    def remove_created_target(self, target):
        self.removed.append(target.value)
        self.targets.pop(target.value, None)

    def restart_service(self, service: str) -> None:
        self.restarts.append(service)
        if self.restart_error:
            raise RuntimeError("restart failed")

    def service_status(self, service: str):
        return {"service": service, "active": True, "state": "active"}


def test_main_parser_exposes_admin_namespace_without_consuming_admin_flags(tmp_path):
    path = tmp_path / "bindings.yaml"
    args = build_parser().parse_args(
        ["admin", "--file", str(path), "--service", "bridge.service", "verify", "alpha", "--json"]
    )

    assert args.command == "admin"
    assert args.admin_file == path
    assert args.service == "bridge.service"
    assert args.admin_args == ["verify", "alpha", "--json"]


def test_admin_inventory_reports_routes_and_tmux_targets(tmp_path, capsys):
    cwd = tmp_path / "alpha"
    cwd.mkdir()
    bindings = tmp_path / "bindings.yaml"
    write_routes(bindings, [route(cwd=str(cwd))])
    runtime = FakeRuntime(
        {
            "alpha:0.0": {
                "state": "running",
                "target": "alpha:0.0",
                "cwd": str(cwd),
                "command": "pi",
                "dead": False,
            }
        }
    )

    assert (
        run_admin_command(
            ["--file", str(bindings), "inventory", "--json"], runtime=runtime
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["routes"][0]["name"] == "alpha"
    assert payload["tmux_targets"][0]["target"] == "alpha:0.0"


def test_admin_contract_install_is_idempotent_and_preserves_existing_text(tmp_path, capsys):
    bindings = tmp_path / "bindings.yaml"
    write_routes(bindings, [route()])
    admin_cwd = tmp_path / "admin"
    admin_cwd.mkdir()
    (admin_cwd / "AGENTS.md").write_text("existing\n", encoding="utf-8")
    argv = [
        "--file",
        str(bindings),
        "--service",
        "bridge.service",
        "install-contract",
        "--cwd",
        str(admin_cwd),
    ]

    assert run_admin_command(argv) == 0
    first = (admin_cwd / "AGENTS.md").read_text(encoding="utf-8")
    assert run_admin_command(argv) == 0
    second = (admin_cwd / "AGENTS.md").read_text(encoding="utf-8")

    assert first == second
    assert first.startswith("existing\n")
    assert first.count("tmuxbot-admin-contract:start") == 1
    assert "Never guess a topic/thread ID" in first
    assert f"--env-file {tmp_path / '.env'}" in first
    assert (admin_cwd / "CLAUDE.md").read_text(encoding="utf-8").count(
        "tmuxbot-admin-contract:start"
    ) == 1
    assert "installed:" in capsys.readouterr().out


def test_telegram_topic_link_parses_private_forum_endpoint():
    topic = parse_telegram_topic_link("https://t.me/c/3799747978/8024/9001")

    assert topic == {
        "channel": "telegram",
        "chat_id": -1003799747978,
        "thread_id": 8024,
        "message_id": 9001,
        "message_link": "https://t.me/c/3799747978/8024/9001",
    }


def test_telegram_topic_link_rejects_public_or_ambiguous_links(capsys, tmp_path):
    bindings = tmp_path / "bindings.yaml"
    write_routes(bindings, [route()])

    exit_code = run_admin_command(
        [
            "--file",
            str(bindings),
            "telegram-topic",
            "--message-link",
            "https://t.me/public_group/123",
            "--json",
        ]
    )

    assert exit_code == 2
    assert "exact form" in capsys.readouterr().out


def test_telegram_topic_cli_outputs_machine_readable_endpoint(tmp_path, capsys):
    bindings = tmp_path / "bindings.yaml"
    write_routes(bindings, [route()])

    assert (
        run_admin_command(
            [
                "--file",
                str(bindings),
                "telegram-topic",
                "--message-link",
                "https://t.me/c/3799747978/8024/9001",
                "--json",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["thread_id"] == 8024


def test_feishu_topic_discovery_returns_exact_root_threads(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("FEISHU_CODEX_APP_ID=app\nFEISHU_CODEX_APP_SECRET=secret\n")
    responses = [
        {"code": 0, "tenant_access_token": "token"},
        {
            "code": 0,
            "data": {
                "items": [
                    {
                        "message_id": "om_root",
                        "root_id": None,
                        "thread_id": "omt_topic",
                        "create_time": "1",
                        "sender": {"sender_type": "user", "id": "ou_boss"},
                        "body": {
                            "content": json.dumps(
                                {"title": "Network", "content": [[{"tag": "text", "text": "Project"}]]}
                            )
                        },
                    },
                    {
                        "message_id": "om_reply",
                        "root_id": "om_root",
                        "thread_id": "omt_topic",
                        "body": {"content": json.dumps({"text": "reply"})},
                    },
                ]
            },
        },
    ]

    class Response:
        def __init__(self, payload):
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    monkeypatch.setattr(
        "tmuxbot.admin_cli.urllib.request.urlopen",
        lambda *_args, **_kwargs: Response(responses.pop(0)),
    )
    monkeypatch.setattr("tmuxbot.admin_cli.json.load", lambda response: response.payload)

    topics = discover_feishu_topics(env_file, "FEISHU_CODEX", "oc_group")

    assert topics == [
        {
            "title": "Network",
            "thread_id": "omt_topic",
            "root_message_id": "om_root",
            "create_time": "1",
            "sender_type": "user",
            "sender_id": "ou_boss",
        }
    ]


def test_bind_topic_plan_does_not_write_or_restart(tmp_path, capsys):
    cwd = tmp_path / "project"
    cwd.mkdir()
    bindings = tmp_path / "bindings.yaml"
    write_routes(bindings, [route(cwd=str(tmp_path / "alpha"))])
    original = bindings.read_text(encoding="utf-8")
    runtime = FakeRuntime(
        {
            "project:0.0": {
                "state": "running",
                "target": "project:0.0",
                "cwd": str(cwd),
                "command": "pi",
                "dead": False,
            }
        }
    )

    exit_code = run_admin_command(
        [
            "--file",
            str(bindings),
            "--service",
            "bridge.service",
            "bind-topic",
            "--name",
            "project",
            "--channel",
            "feishu",
            "--credential",
            "FEISHU_CODEX",
            "--chat-id",
            "oc_group",
            "--thread-id",
            "omt_topic",
            "--tmux-target",
            "project:0.0",
            "--cwd",
            str(cwd),
            "--backend",
            "pi",
            "--mention-required",
            "false",
        ],
        runtime=runtime,
    )

    assert exit_code == 0
    assert bindings.read_text(encoding="utf-8") == original
    assert runtime.restarts == []
    output = capsys.readouterr().out
    assert '"operation": "bind-topic"' in output
    assert "plan only" in output


def test_bind_topic_rejects_tmux_cwd_mismatch(tmp_path, capsys):
    cwd = tmp_path / "project"
    cwd.mkdir()
    bindings = tmp_path / "bindings.yaml"
    write_routes(bindings, [route(cwd=str(tmp_path / "alpha"))])
    runtime = FakeRuntime(
        {
            "project:0.0": {
                "state": "running",
                "target": "project:0.0",
                "cwd": str(tmp_path / "wrong"),
                "command": "pi",
                "dead": False,
            }
        }
    )

    exit_code = run_admin_command(
        [
            "--file",
            str(bindings),
            "bind-topic",
            "--name",
            "project",
            "--channel",
            "feishu",
            "--credential",
            "FEISHU_CODEX",
            "--chat-id",
            "oc_group",
            "--thread-id",
            "omt_topic",
            "--tmux-target",
            "project:0.0",
            "--cwd",
            str(cwd),
            "--backend",
            "pi",
        ],
        runtime=runtime,
    )

    assert exit_code == 2
    assert "cwd mismatch" in capsys.readouterr().out


def test_bind_topic_apply_creates_target_writes_route_and_restarts(tmp_path):
    cwd = tmp_path / "project"
    cwd.mkdir()
    bindings = tmp_path / "bindings.yaml"
    write_routes(bindings, [route(cwd=str(tmp_path / "alpha"))])
    runtime = FakeRuntime()

    exit_code = run_admin_command(
        [
            "--file",
            str(bindings),
            "--service",
            "bridge.service",
            "bind-topic",
            "--name",
            "project",
            "--channel",
            "feishu",
            "--credential",
            "FEISHU_CODEX",
            "--chat-id",
            "oc_group",
            "--thread-id",
            "omt_topic",
            "--tmux-target",
            "project:0.0",
            "--cwd",
            str(cwd),
            "--backend",
            "pi",
            "--create-target",
            "--apply",
        ],
        runtime=runtime,
    )

    assert exit_code == 0
    bound = RouteStore(bindings).inspect("project")
    assert (bound.chat_id, bound.thread_id, bound.tmux_target) == (
        "oc_group",
        "omt_topic",
        "project:0.0",
    )
    assert bound.cwd == cwd
    assert runtime.created == [("project:0.0", cwd)]
    assert runtime.restarts == ["bridge.service"]
    assert bindings.stat().st_mode & 0o777 == 0o600


def test_move_topic_preserves_provider_identity_and_target(tmp_path):
    cwd = tmp_path / "alpha"
    cwd.mkdir()
    transcript = tmp_path / "alpha.jsonl"
    bindings = tmp_path / "bindings.yaml"
    write_routes(
        bindings,
        [
            route(
                cwd=str(cwd),
                transcript_path=str(transcript),
                provider_session_id="session-old",
            )
        ],
    )
    runtime = FakeRuntime(
        {
            "alpha:0.0": {
                "state": "running",
                "target": "alpha:0.0",
                "cwd": str(cwd),
                "command": "pi",
                "dead": False,
            }
        }
    )

    exit_code = run_admin_command(
        [
            "--file",
            str(bindings),
            "--service",
            "bridge.service",
            "move-topic",
            "alpha",
            "--channel",
            "feishu",
            "--chat-id",
            "oc_new",
            "--thread-id",
            "omt_new",
            "--apply",
        ],
        runtime=runtime,
    )

    assert exit_code == 0
    moved = RouteStore(bindings).inspect("alpha")
    assert (moved.chat_id, moved.thread_id) == ("oc_new", "omt_new")
    assert moved.tmux_target == "alpha:0.0"
    assert moved.cwd == cwd
    assert moved.provider_session_id == "session-old"
    assert moved.transcript_path == transcript
    assert runtime.restarts == ["bridge.service"]


def test_move_topic_plan_rejects_duplicate_destination_without_writing(tmp_path, capsys):
    alpha_cwd = tmp_path / "alpha"
    beta_cwd = tmp_path / "beta"
    alpha_cwd.mkdir()
    beta_cwd.mkdir()
    bindings = tmp_path / "bindings.yaml"
    write_routes(
        bindings,
        [
            route(cwd=str(alpha_cwd)),
            route(
                "beta",
                chat_id="oc_taken",
                thread_id="omt_taken",
                tmux_session="beta",
                cwd=str(beta_cwd),
                provider_session_id="session-beta",
                transcript_path=str(tmp_path / "beta.jsonl"),
            ),
        ],
    )
    original = bindings.read_bytes()
    runtime = FakeRuntime()

    exit_code = run_admin_command(
        [
            "--file",
            str(bindings),
            "move-topic",
            "alpha",
            "--channel",
            "feishu",
            "--chat-id",
            "oc_taken",
            "--thread-id",
            "omt_taken",
        ],
        runtime=runtime,
    )

    assert exit_code == 2
    assert "duplicate source" in capsys.readouterr().out
    assert bindings.read_bytes() == original
    assert runtime.restarts == []


def test_apply_rolls_back_yaml_when_service_restart_fails(tmp_path, capsys):
    cwd = tmp_path / "project"
    cwd.mkdir()
    bindings = tmp_path / "bindings.yaml"
    write_routes(bindings, [route(cwd=str(tmp_path / "alpha"))])
    original = bindings.read_bytes()
    runtime = FakeRuntime(
        {
            "project:0.0": {
                "state": "running",
                "target": "project:0.0",
                "cwd": str(cwd),
                "command": "pi",
                "dead": False,
            }
        },
        restart_error=True,
    )

    exit_code = run_admin_command(
        [
            "--file",
            str(bindings),
            "--service",
            "bridge.service",
            "bind-topic",
            "--name",
            "project",
            "--channel",
            "feishu",
            "--credential",
            "FEISHU_CODEX",
            "--chat-id",
            "oc_group",
            "--thread-id",
            "omt_topic",
            "--tmux-target",
            "project:0.0",
            "--cwd",
            str(cwd),
            "--backend",
            "pi",
            "--apply",
        ],
        runtime=runtime,
    )

    assert exit_code == 2
    assert bindings.read_bytes() == original
    assert "restart failed" in capsys.readouterr().out
    assert runtime.restarts == ["bridge.service", "bridge.service"]


def test_verify_json_reports_route_tmux_and_service(tmp_path, capsys):
    cwd = tmp_path / "alpha"
    cwd.mkdir()
    bindings = tmp_path / "bindings.yaml"
    write_routes(bindings, [route(cwd=str(cwd))])
    runtime = FakeRuntime(
        {
            "alpha:0.0": {
                "state": "running",
                "target": "alpha:0.0",
                "cwd": str(cwd),
                "command": "pi",
                "dead": False,
            }
        }
    )

    assert (
        run_admin_command(
            [
                "--file",
                str(bindings),
                "--service",
                "bridge.service",
                "verify",
                "alpha",
                "--json",
            ],
            runtime=runtime,
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["route"]["tmux_target"] == "alpha:0.0"
    assert payload["tmux"]["command"] == "pi"
    assert payload["service"]["active"] is True
