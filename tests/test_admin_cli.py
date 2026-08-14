from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import yaml

from tmuxbot.__main__ import build_parser
from tmuxbot.admin_cli import (
    AdminRuntime,
    create_feishu_topic,
    discover_feishu_topics,
    create_telegram_topic,
    delete_telegram_topic,
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
        "backend": "omp",
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


def test_admin_runtime_treats_empty_tmux_output_as_stopped(monkeypatch):
    runtime = AdminRuntime()
    monkeypatch.setattr(
        runtime,
        "run",
        lambda _argv: type(
            "Result",
            (),
            {"returncode": 0, "stdout": "", "stderr": ""},
        )(),
    )

    status = runtime.target_status(type("Target", (), {"value": "missing:0.0"})())

    assert status == {"state": "stopped", "target": "missing:0.0"}


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
        self.renamed: list[tuple[str, str]] = []
        self.respawned: list[tuple[str, Path]] = []

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

    def rename_session(self, old_name: str, new_name: str) -> None:
        self.renamed.append((old_name, new_name))
        moved = {}
        for key, value in list(self.targets.items()):
            if key.split(":", 1)[0] != old_name:
                continue
            suffix = key.split(":", 1)[1]
            self.targets.pop(key)
            new_key = f"{new_name}:{suffix}"
            moved[new_key] = {**value, "target": new_key}
        self.targets.update(moved)

    def respawn_target(self, target, cwd: Path) -> None:
        self.respawned.append((target.value, cwd))
        status = self.targets[target.value]
        self.targets[target.value] = {**status, "cwd": str(cwd), "command": "bash"}

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
                "command": "omp",
                "dead": False,
            }
        }
    )

    assert run_admin_command(["--file", str(bindings), "inventory", "--json"], runtime=runtime) == 0
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
    assert "dedicated tmuxbot Admin DM management agent" in first
    assert "not an ordinary project" in first
    assert "ADMIN-RUNBOOK.md" in first
    assert "Never guess a topic/thread ID" in first
    assert "do not claim this interface cannot send files" in first
    assert "[download](</absolute/path/report.pdf>)" in first
    assert f"--env-file {tmp_path / '.env'}" in first
    assert (admin_cwd / "CLAUDE.md").read_text(encoding="utf-8").count(
        "tmuxbot-admin-contract:start"
    ) == 1
    runbook = (admin_cwd / "ADMIN-RUNBOOK.md").read_text(encoding="utf-8")
    assert "How this conversation runs" in runbook
    assert str(bindings.resolve()) in runbook
    manifest = json.loads((admin_cwd / "tmuxbot-admin-context.json").read_text(encoding="utf-8"))
    assert manifest["schema_version"] == 1
    assert manifest["admin_cwd"] == str(admin_cwd.resolve())
    assert set(manifest["files"]) == {"AGENTS.md", "CLAUDE.md", "ADMIN-RUNBOOK.md"}
    assert admin_cwd.stat().st_mode & 0o777 == 0o700
    assert all(
        path.stat().st_mode & 0o777 == 0o600
        for path in (
            admin_cwd / "AGENTS.md",
            admin_cwd / "CLAUDE.md",
            admin_cwd / "ADMIN-RUNBOOK.md",
            admin_cwd / "tmuxbot-admin-context.json",
        )
    )
    assert "installed:" in capsys.readouterr().out


def test_admin_context_verification_detects_stale_contract(tmp_path, capsys):
    bindings = tmp_path / "bindings.yaml"
    write_routes(bindings, [route()])
    admin_cwd = tmp_path / "admin"
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
    (admin_cwd / "AGENTS.md").write_text("tampered\n", encoding="utf-8")

    exit_code = run_admin_command(
        [
            "--file",
            str(bindings),
            "--service",
            "bridge.service",
            "verify-context",
            "--cwd",
            str(admin_cwd),
            "--json",
        ]
    )

    assert exit_code == 2
    payload = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert payload["ok"] is False
    assert any("AGENTS.md" in error for error in payload["errors"])


def test_admin_contract_install_creates_default_dedicated_workspace(monkeypatch, tmp_path, capsys):
    bindings = tmp_path / "bindings.yaml"
    write_routes(bindings, [route()])
    data_home = tmp_path / "share"
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))

    assert run_admin_command(["--file", str(bindings), "install-contract"]) == 0

    admin_cwd = data_home / "tmuxbot/admin"
    assert (admin_cwd / "AGENTS.md").is_file()
    assert str(admin_cwd) in capsys.readouterr().out


def test_telegram_topic_link_parses_private_forum_endpoint():
    topic = parse_telegram_topic_link("https://t.me/c/3799747978/8024/9001")

    assert topic == {
        "channel": "telegram",
        "chat_id": -1003799747978,
        "thread_id": 8024,
        "message_id": 9001,
        "message_link": "https://t.me/c/3799747978/8024/9001",
    }


def test_telegram_topic_link_accepts_topic_url_without_message_id():
    topic = parse_telegram_topic_link("https://t.me/c/3799747978/42337")

    assert topic == {
        "channel": "telegram",
        "chat_id": -1003799747978,
        "thread_id": 42337,
        "message_id": None,
        "message_link": "https://t.me/c/3799747978/42337",
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
    assert "private forum topic or message link" in capsys.readouterr().out


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
                                {
                                    "title": "Network",
                                    "content": [[{"tag": "text", "text": "Project"}]],
                                }
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


def test_create_telegram_topic_returns_exact_endpoint(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("TG_CODEX_BOT_TOKEN=token\n")
    requests = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    def urlopen(request, **_kwargs):
        requests.append(request)
        return Response()

    monkeypatch.setattr("tmuxbot.admin_cli.urllib.request.urlopen", urlopen)
    monkeypatch.setattr(
        "tmuxbot.admin_cli.json.load",
        lambda _response: {
            "ok": True,
            "result": {"message_thread_id": 42337, "name": "CliproxyApi"},
        },
    )

    topic = create_telegram_topic(env_file, "TG_CODEX_BOT_TOKEN", -1003799747978, "CliproxyApi")

    assert topic == {
        "title": "CliproxyApi",
        "chat_id": -1003799747978,
        "thread_id": 42337,
        "root_message_id": None,
    }
    assert requests[0].full_url.endswith("/bottoken/createForumTopic")


def test_delete_telegram_topic_uses_delete_forum_topic(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("TG_CODEX_BOT_TOKEN=token\n")
    requests = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    monkeypatch.setattr(
        "tmuxbot.admin_cli.urllib.request.urlopen",
        lambda request, **_kwargs: requests.append(request) or Response(),
    )
    monkeypatch.setattr(
        "tmuxbot.admin_cli.json.load", lambda _response: {"ok": True, "result": True}
    )

    delete_telegram_topic(env_file, "TG_CODEX_BOT_TOKEN", -1003799747978, 42337)

    assert requests[0].full_url.endswith("/bottoken/deleteForumTopic")


def test_create_telegram_topic_plan_has_no_remote_or_local_side_effects(
    monkeypatch, tmp_path, capsys
):
    cwd = tmp_path / "project-omp"
    cwd.mkdir()
    bindings = tmp_path / "bindings.yaml"
    write_routes(bindings, [route(cwd=str(tmp_path / "alpha"))])
    original = bindings.read_bytes()
    runtime = FakeRuntime()
    monkeypatch.setattr(
        "tmuxbot.admin_cli.create_telegram_topic",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("remote create")),
    )

    exit_code = run_admin_command(
        [
            "--file",
            str(bindings),
            "--service",
            "bridge.service",
            "create-topic",
            "--env-file",
            str(tmp_path / ".env"),
            "--name",
            "project-omp",
            "--channel",
            "telegram",
            "--credential",
            "TG_CODEX_BOT_TOKEN",
            "--chat-id",
            "-1003799747978",
            "--topic-title",
            "CliproxyApi",
            "--tmux-target",
            "project-omp:0.0",
            "--cwd",
            str(cwd),
            "--backend",
            "omp",
            "--create-target",
        ],
        runtime=runtime,
    )

    assert exit_code == 0
    assert bindings.read_bytes() == original
    assert runtime.created == []
    assert runtime.restarts == []
    output = capsys.readouterr().out
    assert '"operation": "create-topic"' in output
    assert '"channel": "telegram"' in output
    assert "plan only" in output


def test_create_telegram_topic_apply_creates_endpoint_target_and_route(monkeypatch, tmp_path):
    cwd = tmp_path / "project-omp"
    cwd.mkdir()
    bindings = tmp_path / "bindings.yaml"
    write_routes(bindings, [route(cwd=str(tmp_path / "alpha"))])
    runtime = FakeRuntime()
    deleted: list[tuple[Path, str, int, int]] = []
    monkeypatch.setattr(
        "tmuxbot.admin_cli.create_telegram_topic",
        lambda *_args, **_kwargs: {
            "title": "CliproxyApi",
            "chat_id": -1003799747978,
            "thread_id": 42337,
            "root_message_id": None,
        },
    )
    monkeypatch.setattr(
        "tmuxbot.admin_cli.delete_telegram_topic",
        lambda env_file, credential, chat_id, thread_id: deleted.append(
            (env_file, credential, chat_id, thread_id)
        ),
    )

    exit_code = run_admin_command(
        [
            "--file",
            str(bindings),
            "--service",
            "bridge.service",
            "create-topic",
            "--env-file",
            str(tmp_path / ".env"),
            "--name",
            "project-omp",
            "--channel",
            "telegram",
            "--credential",
            "TG_CODEX_BOT_TOKEN",
            "--chat-id",
            "-1003799747978",
            "--topic-title",
            "CliproxyApi",
            "--tmux-target",
            "project-omp:0.0",
            "--cwd",
            str(cwd),
            "--backend",
            "omp",
            "--mention-required",
            "false",
            "--create-target",
            "--apply",
        ],
        runtime=runtime,
    )

    assert exit_code == 0
    bound = RouteStore(bindings).inspect("project-omp")
    assert (bound.chat_id, bound.thread_id, bound.thread_root_message_id) == (
        -1003799747978,
        42337,
        None,
    )
    assert runtime.created == [("project-omp:0.0", cwd)]
    assert runtime.restarts == ["bridge.service"]
    assert deleted == []


def test_create_feishu_topic_returns_exact_endpoint(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("FEISHU_CODEX_APP_ID=app\nFEISHU_CODEX_APP_SECRET=secret\n")
    responses = [
        {"code": 0, "tenant_access_token": "token"},
        {"code": 0, "data": {"message_id": "om_root"}},
        {
            "code": 0,
            "data": {
                "items": [
                    {
                        "message_id": "om_root",
                        "root_id": None,
                        "thread_id": "omt_topic",
                        "create_time": "1",
                        "sender": {"sender_type": "app", "id": "cli_app"},
                        "body": {"content": json.dumps({"text": "Network branch"})},
                    }
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

    topic = create_feishu_topic(
        env_file, "FEISHU_CODEX", "oc_group", "Network branch", poll_interval=0
    )

    assert topic == {
        "title": "Network branch",
        "chat_id": "oc_group",
        "thread_id": "omt_topic",
        "root_message_id": "om_root",
    }


def test_create_feishu_topic_plan_has_no_remote_or_local_side_effects(
    monkeypatch, tmp_path, capsys
):
    cwd = tmp_path / "project-omp"
    cwd.mkdir()
    bindings = tmp_path / "bindings.yaml"
    write_routes(bindings, [route(cwd=str(tmp_path / "alpha"))])
    original = bindings.read_bytes()
    runtime = FakeRuntime()
    monkeypatch.setattr(
        "tmuxbot.admin_cli.create_feishu_topic",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("remote create")),
    )

    exit_code = run_admin_command(
        [
            "--file",
            str(bindings),
            "--service",
            "bridge.service",
            "create-feishu-topic",
            "--env-file",
            str(tmp_path / ".env"),
            "--name",
            "project-omp",
            "--credential",
            "FEISHU_CODEX",
            "--chat-id",
            "oc_group",
            "--topic-title",
            "Network branch",
            "--tmux-target",
            "project-omp:0.0",
            "--cwd",
            str(cwd),
            "--backend",
            "omp",
            "--create-target",
        ],
        runtime=runtime,
    )

    assert exit_code == 0
    assert bindings.read_bytes() == original
    assert runtime.created == []
    assert runtime.restarts == []
    output = capsys.readouterr().out
    assert '"operation": "create-topic"' in output
    assert '"title": "Network branch"' in output
    assert "plan only" in output


def test_create_feishu_topic_apply_creates_endpoint_target_and_route(monkeypatch, tmp_path):
    cwd = tmp_path / "project-omp"
    cwd.mkdir()
    bindings = tmp_path / "bindings.yaml"
    write_routes(bindings, [route(cwd=str(tmp_path / "alpha"))])
    runtime = FakeRuntime()
    deleted: list[tuple[Path, str, str]] = []
    monkeypatch.setattr(
        "tmuxbot.admin_cli.create_feishu_topic",
        lambda *_args, **_kwargs: {
            "title": "Network branch",
            "chat_id": "oc_group",
            "thread_id": "omt_topic",
            "root_message_id": "om_root",
        },
    )
    monkeypatch.setattr(
        "tmuxbot.admin_cli.delete_feishu_message",
        lambda env_file, credential, message_id: deleted.append((env_file, credential, message_id)),
    )

    exit_code = run_admin_command(
        [
            "--file",
            str(bindings),
            "--service",
            "bridge.service",
            "create-feishu-topic",
            "--env-file",
            str(tmp_path / ".env"),
            "--name",
            "project-omp",
            "--credential",
            "FEISHU_CODEX",
            "--chat-id",
            "oc_group",
            "--topic-title",
            "Network branch",
            "--tmux-target",
            "project-omp:0.0",
            "--cwd",
            str(cwd),
            "--backend",
            "omp",
            "--mention-required",
            "false",
            "--create-target",
            "--apply",
        ],
        runtime=runtime,
    )

    assert exit_code == 0
    bound = RouteStore(bindings).inspect("project-omp")
    assert (bound.chat_id, bound.thread_id, bound.thread_root_message_id) == (
        "oc_group",
        "omt_topic",
        "om_root",
    )
    assert bound.tmux_target == "project-omp:0.0"
    assert runtime.created == [("project-omp:0.0", cwd)]
    assert runtime.restarts == ["bridge.service"]
    assert deleted == []


def test_create_feishu_topic_apply_removes_topic_and_target_on_bind_failure(
    monkeypatch, tmp_path, capsys
):
    cwd = tmp_path / "project-omp"
    cwd.mkdir()
    bindings = tmp_path / "bindings.yaml"
    write_routes(bindings, [route(cwd=str(tmp_path / "alpha"))])
    original = bindings.read_bytes()
    runtime = FakeRuntime(restart_error=True)
    deleted: list[str] = []
    monkeypatch.setattr(
        "tmuxbot.admin_cli.create_feishu_topic",
        lambda *_args, **_kwargs: {
            "title": "Network branch",
            "chat_id": "oc_group",
            "thread_id": "omt_topic",
            "root_message_id": "om_root",
        },
    )
    monkeypatch.setattr(
        "tmuxbot.admin_cli.delete_feishu_message",
        lambda _env_file, _credential, message_id: deleted.append(message_id),
    )

    exit_code = run_admin_command(
        [
            "--file",
            str(bindings),
            "--service",
            "bridge.service",
            "create-feishu-topic",
            "--env-file",
            str(tmp_path / ".env"),
            "--name",
            "project-omp",
            "--credential",
            "FEISHU_CODEX",
            "--chat-id",
            "oc_group",
            "--topic-title",
            "Network branch",
            "--tmux-target",
            "project-omp:0.0",
            "--cwd",
            str(cwd),
            "--backend",
            "omp",
            "--create-target",
            "--apply",
        ],
        runtime=runtime,
    )

    assert exit_code == 2
    assert bindings.read_bytes() == original
    assert runtime.removed == ["project-omp:0.0"]
    assert deleted == ["om_root"]
    assert "restart failed" in capsys.readouterr().out


def test_provision_project_plan_resolves_telegram_topic_link_and_defaults_target(tmp_path, capsys):
    cwd = tmp_path / "project-omp"
    cwd.mkdir()
    bindings = tmp_path / "bindings.yaml"
    write_routes(bindings, [route(cwd=str(tmp_path / "alpha"))])
    original = bindings.read_bytes()
    runtime = FakeRuntime()

    exit_code = run_admin_command(
        [
            "--file",
            str(bindings),
            "--service",
            "bridge.service",
            "provision-project",
            "--name",
            "cliproxyapi-omp",
            "--channel",
            "telegram",
            "--credential",
            "TG_CODEX_BOT_TOKEN",
            "--topic-link",
            "https://t.me/c/3799747978/42337",
            "--cwd",
            str(cwd),
            "--backend",
            "omp",
        ],
        runtime=runtime,
    )

    assert exit_code == 0
    assert bindings.read_bytes() == original
    assert runtime.created == []
    assert runtime.restarts == []
    payload = json.loads(capsys.readouterr().out.split("\nplan only:", 1)[0])
    assert payload["operation"] == "provision-project"
    assert payload["endpoint"] == {
        "mode": "existing",
        "channel": "telegram",
        "credential": "TG_CODEX_BOT_TOKEN",
        "chat_id": -1003799747978,
        "thread_id": 42337,
        "thread_root_message_id": None,
        "topic_title": None,
    }
    assert payload["route"]["tmux_target"] == "cliproxyapi-omp:0.0"
    assert payload["target_action"] == "create"


def test_provision_project_rejects_mismatched_omp_tmux_session(tmp_path, capsys):
    cwd = tmp_path / "project"
    cwd.mkdir()
    bindings = tmp_path / "bindings.yaml"
    write_routes(bindings, [route(cwd=str(tmp_path / "alpha"))])

    exit_code = run_admin_command(
        [
            "--file",
            str(bindings),
            "provision-project",
            "--name",
            "project-omp",
            "--channel",
            "telegram",
            "--credential",
            "TG_CODEX_BOT_TOKEN",
            "--topic-link",
            "https://t.me/c/3799747978/42337",
            "--tmux-target",
            "other-omp:0.0",
            "--cwd",
            str(cwd),
            "--backend",
            "omp",
        ]
    )

    assert exit_code == 2
    assert "must equal its route name" in capsys.readouterr().out


def test_provision_project_apply_binds_existing_telegram_topic(tmp_path):
    cwd = tmp_path / "project-omp"
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
            "provision-project",
            "--name",
            "cliproxyapi-omp",
            "--channel",
            "telegram",
            "--credential",
            "TG_CODEX_BOT_TOKEN",
            "--topic-link",
            "https://t.me/c/3799747978/42337",
            "--cwd",
            str(cwd),
            "--backend",
            "omp",
            "--apply",
        ],
        runtime=runtime,
    )

    assert exit_code == 0
    bound = RouteStore(bindings).inspect("cliproxyapi-omp")
    assert (bound.chat_id, bound.thread_id, bound.thread_root_message_id) == (
        -1003799747978,
        42337,
        None,
    )
    assert bound.tmux_target == "cliproxyapi-omp:0.0"
    assert runtime.created == [("cliproxyapi-omp:0.0", cwd)]
    assert runtime.restarts == ["bridge.service"]


def test_provision_project_apply_creates_feishu_topic_and_route(monkeypatch, tmp_path):
    cwd = tmp_path / "project-omp"
    cwd.mkdir()
    bindings = tmp_path / "bindings.yaml"
    write_routes(bindings, [route(cwd=str(tmp_path / "alpha"))])
    runtime = FakeRuntime()
    monkeypatch.setattr(
        "tmuxbot.admin_cli.create_feishu_topic",
        lambda *_args, **_kwargs: {
            "title": "Network branch",
            "chat_id": "oc_group",
            "thread_id": "omt_topic",
            "root_message_id": "om_root",
        },
    )

    exit_code = run_admin_command(
        [
            "--file",
            str(bindings),
            "--service",
            "bridge.service",
            "provision-project",
            "--env-file",
            str(tmp_path / ".env"),
            "--name",
            "network-branch-omp",
            "--channel",
            "feishu",
            "--credential",
            "FEISHU_CODEX",
            "--chat-id",
            "oc_group",
            "--topic-title",
            "Network branch",
            "--cwd",
            str(cwd),
            "--backend",
            "omp",
            "--apply",
        ],
        runtime=runtime,
    )

    assert exit_code == 0
    bound = RouteStore(bindings).inspect("network-branch-omp")
    assert (bound.chat_id, bound.thread_id, bound.thread_root_message_id) == (
        "oc_group",
        "omt_topic",
        "om_root",
    )
    assert bound.tmux_target == "network-branch-omp:0.0"


def test_provision_project_rejects_ambiguous_topic_intent(tmp_path, capsys):
    cwd = tmp_path / "project-omp"
    cwd.mkdir()
    bindings = tmp_path / "bindings.yaml"
    write_routes(bindings, [route(cwd=str(tmp_path / "alpha"))])

    exit_code = run_admin_command(
        [
            "--file",
            str(bindings),
            "provision-project",
            "--name",
            "project-omp",
            "--channel",
            "telegram",
            "--credential",
            "TG_CODEX_BOT_TOKEN",
            "--chat-id",
            "-1003799747978",
            "--thread-id",
            "42337",
            "--topic-title",
            "Duplicate intent",
            "--cwd",
            str(cwd),
            "--backend",
            "omp",
        ]
    )

    assert exit_code == 2
    assert "exactly one topic intent" in capsys.readouterr().out


def test_rename_project_plan_has_no_side_effects(tmp_path, capsys):
    old_cwd = tmp_path / "omp-agent"
    old_cwd.mkdir()
    new_cwd = tmp_path / "dida-todo-omp"
    bindings = tmp_path / "bindings.yaml"
    write_routes(bindings, [route("omp-agent", cwd=str(old_cwd))])
    original = bindings.read_bytes()
    runtime = FakeRuntime(
        {
            "omp-agent:0.0": {
                "state": "running",
                "target": "omp-agent:0.0",
                "cwd": str(old_cwd),
                "command": "omp",
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
            "rename-project",
            "omp-agent",
            "--new-name",
            "dida-todo-omp",
            "--new-cwd",
            str(new_cwd),
        ],
        runtime=runtime,
    )

    assert exit_code == 0
    assert bindings.read_bytes() == original
    assert old_cwd.is_dir() and not new_cwd.exists()
    assert runtime.renamed == []
    assert runtime.respawned == []
    payload = json.loads(capsys.readouterr().out.split("\nplan only:", 1)[0])
    assert payload["operation"] == "rename-project"
    assert payload["before"]["tmux_target"] == "omp-agent:0.0"
    assert payload["after"]["tmux_target"] == "dida-todo-omp:0.0"
    assert payload["after"]["provider_session_id"] is None


def test_rename_project_apply_moves_cwd_session_and_route(tmp_path):
    old_cwd = tmp_path / "omp-agent"
    old_cwd.mkdir()
    (old_cwd / "marker").write_text("ok", encoding="utf-8")
    new_cwd = tmp_path / "dida-todo-omp"
    bindings = tmp_path / "bindings.yaml"
    write_routes(bindings, [route("omp-agent", cwd=str(old_cwd))])
    runtime = FakeRuntime(
        {
            "omp-agent:0.0": {
                "state": "running",
                "target": "omp-agent:0.0",
                "cwd": str(old_cwd),
                "command": "omp",
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
            "rename-project",
            "omp-agent",
            "--new-name",
            "dida-todo-omp",
            "--new-cwd",
            str(new_cwd),
            "--apply",
        ],
        runtime=runtime,
    )

    assert exit_code == 0
    assert not old_cwd.exists()
    assert (new_cwd / "marker").read_text(encoding="utf-8") == "ok"
    bound = RouteStore(bindings).inspect("dida-todo-omp")
    assert bound.cwd == new_cwd
    assert bound.tmux_target == "dida-todo-omp:0.0"
    assert bound.provider_session_id is None
    assert bound.transcript_path is None
    assert runtime.renamed == [("omp-agent", "dida-todo-omp")]
    assert runtime.respawned == [("dida-todo-omp:0.0", new_cwd)]
    assert runtime.restarts == ["bridge.service"]


def test_rename_project_keep_cwd_preserves_omp_session_identity(tmp_path):
    cwd = tmp_path / "project"
    cwd.mkdir()
    transcript = tmp_path / "project.jsonl"
    bindings = tmp_path / "bindings.yaml"
    write_routes(
        bindings,
        [
            route(
                "legacy",
                cwd=str(cwd),
                provider_session_id="session-old",
                transcript_path=str(transcript),
            )
        ],
    )
    runtime = FakeRuntime(
        {
            "legacy:0.0": {
                "state": "running",
                "target": "legacy:0.0",
                "cwd": str(cwd),
                "command": "omp",
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
            "rename-project",
            "legacy",
            "--new-name",
            "project-omp",
            "--keep-cwd",
            "--apply",
        ],
        runtime=runtime,
    )

    assert exit_code == 0
    bound = RouteStore(bindings).inspect("project-omp")
    assert bound.cwd == cwd
    assert bound.provider_session_id == "session-old"
    assert bound.transcript_path == transcript
    assert runtime.renamed == [("legacy", "project-omp")]
    assert runtime.respawned == [("project-omp:0.0", cwd)]
    assert runtime.restarts == ["bridge.service"]


def test_rename_project_rolls_back_filesystem_session_and_route_on_restart_failure(tmp_path):
    old_cwd = tmp_path / "omp-agent"
    old_cwd.mkdir()
    new_cwd = tmp_path / "dida-todo-omp"
    bindings = tmp_path / "bindings.yaml"
    write_routes(bindings, [route("omp-agent", cwd=str(old_cwd))])
    original = bindings.read_bytes()
    runtime = FakeRuntime(
        {
            "omp-agent:0.0": {
                "state": "running",
                "target": "omp-agent:0.0",
                "cwd": str(old_cwd),
                "command": "omp",
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
            "rename-project",
            "omp-agent",
            "--new-name",
            "dida-todo-omp",
            "--new-cwd",
            str(new_cwd),
            "--apply",
        ],
        runtime=runtime,
    )

    assert exit_code == 2
    assert bindings.read_bytes() == original
    assert old_cwd.is_dir() and not new_cwd.exists()
    assert "omp-agent:0.0" in runtime.targets
    assert "dida-todo-omp:0.0" not in runtime.targets
    assert runtime.renamed == [("omp-agent", "dida-todo-omp"), ("dida-todo-omp", "omp-agent")]


def test_bind_topic_plan_does_not_write_or_restart(tmp_path, capsys):
    cwd = tmp_path / "project-omp"
    cwd.mkdir()
    bindings = tmp_path / "bindings.yaml"
    write_routes(bindings, [route(cwd=str(tmp_path / "alpha"))])
    original = bindings.read_text(encoding="utf-8")
    runtime = FakeRuntime(
        {
            "project-omp:0.0": {
                "state": "running",
                "target": "project-omp:0.0",
                "cwd": str(cwd),
                "command": "omp",
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
            "project-omp",
            "--channel",
            "feishu",
            "--credential",
            "FEISHU_CODEX",
            "--chat-id",
            "oc_group",
            "--thread-id",
            "omt_topic",
            "--thread-root-message-id",
            "om_root",
            "--tmux-target",
            "project-omp:0.0",
            "--cwd",
            str(cwd),
            "--backend",
            "omp",
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


def test_bind_topic_rejects_feishu_topic_without_durable_root_before_side_effects(tmp_path, capsys):
    cwd = tmp_path / "project-omp"
    cwd.mkdir()
    bindings = tmp_path / "bindings.yaml"
    write_routes(bindings, [route(cwd=str(tmp_path / "alpha"))])
    original = bindings.read_bytes()
    runtime = FakeRuntime()

    exit_code = run_admin_command(
        [
            "--file",
            str(bindings),
            "bind-topic",
            "--name",
            "project-omp",
            "--channel",
            "feishu",
            "--credential",
            "FEISHU_CODEX",
            "--chat-id",
            "oc_group",
            "--thread-id",
            "omt_topic",
            "--tmux-target",
            "project-omp:0.0",
            "--cwd",
            str(cwd),
            "--backend",
            "omp",
            "--create-target",
            "--apply",
        ],
        runtime=runtime,
    )

    assert exit_code == 2
    assert "--thread-root-message-id" in capsys.readouterr().out
    assert bindings.read_bytes() == original
    assert runtime.created == []
    assert runtime.restarts == []


def test_bind_topic_rejects_tmux_cwd_mismatch(tmp_path, capsys):
    cwd = tmp_path / "project-omp"
    cwd.mkdir()
    bindings = tmp_path / "bindings.yaml"
    write_routes(bindings, [route(cwd=str(tmp_path / "alpha"))])
    runtime = FakeRuntime(
        {
            "project-omp:0.0": {
                "state": "running",
                "target": "project-omp:0.0",
                "cwd": str(tmp_path / "wrong"),
                "command": "omp",
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
            "project-omp",
            "--channel",
            "feishu",
            "--credential",
            "FEISHU_CODEX",
            "--chat-id",
            "oc_group",
            "--thread-id",
            "omt_topic",
            "--thread-root-message-id",
            "om_root",
            "--tmux-target",
            "project-omp:0.0",
            "--cwd",
            str(cwd),
            "--backend",
            "omp",
        ],
        runtime=runtime,
    )

    assert exit_code == 2
    assert "cwd mismatch" in capsys.readouterr().out


def test_bind_topic_apply_creates_target_writes_route_and_restarts(tmp_path):
    cwd = tmp_path / "project-omp"
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
            "project-omp",
            "--channel",
            "feishu",
            "--credential",
            "FEISHU_CODEX",
            "--chat-id",
            "oc_group",
            "--thread-id",
            "omt_topic",
            "--thread-root-message-id",
            "om_root",
            "--tmux-target",
            "project-omp:0.0",
            "--cwd",
            str(cwd),
            "--backend",
            "omp",
            "--create-target",
            "--apply",
        ],
        runtime=runtime,
    )

    assert exit_code == 0
    bound = RouteStore(bindings).inspect("project-omp")
    assert (bound.chat_id, bound.thread_id, bound.tmux_target) == (
        "oc_group",
        "omt_topic",
        "project-omp:0.0",
    )
    assert bound.cwd == cwd
    assert runtime.created == [("project-omp:0.0", cwd)]
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
                "command": "omp",
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
            "--thread-root-message-id",
            "om_new_root",
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
    assert moved.thread_root_message_id == "om_new_root"
    assert runtime.restarts == ["bridge.service"]


def test_bind_apply_rejects_duplicate_endpoint_before_creating_target(tmp_path, capsys):
    existing_cwd = tmp_path / "existing"
    requested_cwd = tmp_path / "requested"
    existing_cwd.mkdir()
    requested_cwd.mkdir()
    bindings = tmp_path / "bindings.yaml"
    write_routes(bindings, [route(cwd=str(existing_cwd))])
    original = bindings.read_bytes()
    runtime = FakeRuntime()

    exit_code = run_admin_command(
        [
            "--file",
            str(bindings),
            "bind-topic",
            "--name",
            "duplicate-omp",
            "--channel",
            "feishu",
            "--credential",
            "FEISHU_CODEX",
            "--chat-id",
            "oc_old",
            "--thread-id",
            "omt_old",
            "--thread-root-message-id",
            "om_old_root",
            "--tmux-target",
            "duplicate-omp:0.0",
            "--cwd",
            str(requested_cwd),
            "--backend",
            "omp",
            "--mention-required",
            "false",
            "--create-target",
            "--apply",
        ],
        runtime=runtime,
    )

    assert exit_code == 2
    assert "duplicate source" in capsys.readouterr().out
    assert bindings.read_bytes() == original
    assert runtime.created == []
    assert runtime.restarts == []


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
            "--thread-root-message-id",
            "om_taken_root",
        ],
        runtime=runtime,
    )

    assert exit_code == 2
    assert "duplicate source" in capsys.readouterr().out
    assert bindings.read_bytes() == original
    assert runtime.restarts == []


def test_apply_rolls_back_yaml_when_service_restart_fails(tmp_path, capsys):
    cwd = tmp_path / "project-omp"
    cwd.mkdir()
    bindings = tmp_path / "bindings.yaml"
    write_routes(bindings, [route(cwd=str(tmp_path / "alpha"))])
    original = bindings.read_bytes()
    runtime = FakeRuntime(
        {
            "project-omp:0.0": {
                "state": "running",
                "target": "project-omp:0.0",
                "cwd": str(cwd),
                "command": "omp",
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
            "project-omp",
            "--channel",
            "feishu",
            "--credential",
            "FEISHU_CODEX",
            "--chat-id",
            "oc_group",
            "--thread-id",
            "omt_topic",
            "--thread-root-message-id",
            "om_root",
            "--tmux-target",
            "project-omp:0.0",
            "--cwd",
            str(cwd),
            "--backend",
            "omp",
            "--apply",
        ],
        runtime=runtime,
    )

    assert exit_code == 2
    assert bindings.read_bytes() == original
    assert "restart failed" in capsys.readouterr().out
    assert runtime.restarts == ["bridge.service", "bridge.service"]


def test_adopt_omp_session_plans_then_applies_verified_identity(tmp_path, capsys, monkeypatch):
    cwd = tmp_path / "alpha"
    cwd.mkdir()
    session = tmp_path / "new-omp-session.jsonl"
    session.write_text(
        json.dumps(
            {
                "type": "session",
                "id": "omp-session-new",
                "cwd": str(cwd),
            }
        )
        + "\n",
        encoding="utf-8",
    )
    bindings = tmp_path / "bindings.yaml"
    write_routes(bindings, [route(cwd=str(cwd))])
    runtime = FakeRuntime(
        {
            "alpha:0.0": {
                "state": "running",
                "target": "alpha:0.0",
                "cwd": str(cwd),
                "command": "omp",
                "dead": False,
            }
        }
    )
    argv = [
        "--file",
        str(bindings),
        "--service",
        "bridge.service",
        "adopt-omp-session",
        "alpha",
        "--session-file",
        str(session),
    ]

    monkeypatch.setattr("tmuxbot.admin_cli.read_handoff", lambda *_args: None)
    assert run_admin_command(argv, runtime=runtime) == 2
    assert "no matching managed identity sidecar" in capsys.readouterr().out
    monkeypatch.setattr(
        "tmuxbot.admin_cli.read_handoff",
        lambda *_args: SimpleNamespace(
            session_id="omp-session-new",
            transcript_path=session.resolve(),
        ),
    )

    assert run_admin_command(argv, runtime=runtime) == 0
    planned = json.loads(capsys.readouterr().out.split("\nplan only:", 1)[0])
    assert planned["before"]["provider_session_id"] == "session-old"
    assert planned["after"] == {
        "provider_session_id": "omp-session-new",
        "transcript_path": str(session),
    }
    assert runtime.restarts == []

    assert run_admin_command([*argv, "--apply"], runtime=runtime) == 0
    adopted = RouteStore(bindings).inspect("alpha")
    assert adopted.provider_session_id == "omp-session-new"
    assert adopted.transcript_path == session
    assert runtime.restarts == ["bridge.service"]


def test_adopt_omp_session_rejects_a_different_cwd(tmp_path, capsys):
    cwd = tmp_path / "alpha"
    cwd.mkdir()
    session = tmp_path / "wrong.jsonl"
    session.write_text(
        json.dumps({"type": "session", "id": "omp-session-wrong", "cwd": str(tmp_path)}) + "\n",
        encoding="utf-8",
    )
    bindings = tmp_path / "bindings.yaml"
    write_routes(bindings, [route(cwd=str(cwd))])
    runtime = FakeRuntime(
        {
            "alpha:0.0": {
                "state": "running",
                "target": "alpha:0.0",
                "cwd": str(cwd),
                "command": "omp",
                "dead": False,
            }
        }
    )

    assert (
        run_admin_command(
            [
                "--file",
                str(bindings),
                "adopt-omp-session",
                "alpha",
                "--session-file",
                str(session),
            ],
            runtime=runtime,
        )
        == 2
    )
    assert "OMP session cwd mismatch" in capsys.readouterr().out
    assert runtime.restarts == []


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
                "command": "omp",
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
    assert payload["tmux"]["command"] == "omp"
    assert payload["service"]["active"] is True
