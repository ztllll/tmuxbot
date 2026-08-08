from __future__ import annotations

import json
from pathlib import Path

from tmuxbot.doctor import DoctorReport, render_report, run_doctor
from tmuxbot.paths import RuntimePaths


def test_doctor_never_exposes_secrets(tmp_path: Path) -> None:
    paths = RuntimePaths.discover({}, home=tmp_path)
    report = run_doctor(
        paths,
        {
            "TG_BOT_TOKEN": "123:secret",
            "FEISHU_APP_SECRET": "very-secret",
            "TMUXBOT_WEB_SETUP_TOKEN": "setup-secret",
            "PATH": "",
        },
    )
    rendered = render_report(report, as_json=True)
    assert "123:secret" not in rendered
    assert "very-secret" not in rendered
    assert "setup-secret" not in rendered
    parsed = json.loads(rendered)
    assert parsed["schema_version"] == 1
    assert isinstance(parsed["checks"], list)


def test_missing_optional_providers_are_warnings(tmp_path: Path) -> None:
    paths = RuntimePaths.discover({}, home=tmp_path)
    report = run_doctor(paths, {"PATH": ""})
    providers = [
        item
        for item in report.checks
        if item.name in {"provider:claude", "provider:codex", "provider:pi"}
    ]
    assert providers
    assert all(item.status == "warning" for item in providers)
    assert isinstance(report, DoctorReport)


def test_doctor_warns_when_tmux_extended_keys_are_disabled(tmp_path, monkeypatch):
    paths = RuntimePaths.discover({}, home=tmp_path)
    monkeypatch.setattr(
        "tmuxbot.doctor.subprocess.run",
        lambda argv, **kwargs: type(
            "Completed",
            (),
            {
                "returncode": 0,
                "stdout": (
                    "tmux 3.4\n"
                    if argv[0].endswith("tmux") and len(argv) == 2
                    else ("off\n" if argv[-1] == "extended-keys" else "xterm\n")
                ),
                "stderr": "",
            },
        )(),
    )
    monkeypatch.setattr(
        "tmuxbot.doctor.shutil.which",
        lambda name, path=None: f"/usr/bin/{name}",
    )

    report = run_doctor(paths, {"PATH": "/usr/bin"})

    check = next(item for item in report.checks if item.name == "tmux:extended_keys")
    assert check.status == "warning"
    assert "Pi" in check.summary
    assert check.details == {"value": "off", "format": "xterm"}


def test_doctor_accepts_pi_extended_key_pair(tmp_path, monkeypatch):
    paths = RuntimePaths.discover({}, home=tmp_path)

    def run(argv, **kwargs):
        if len(argv) == 2:
            stdout = "tmux 3.4\n"
        elif argv[-1] == "extended-keys":
            stdout = "on\n"
        else:
            stdout = "csi-u\n"
        return type(
            "Completed",
            (),
            {"returncode": 0, "stdout": stdout, "stderr": ""},
        )()

    monkeypatch.setattr("tmuxbot.doctor.subprocess.run", run)
    monkeypatch.setattr(
        "tmuxbot.doctor.shutil.which",
        lambda name, path=None: f"/usr/bin/{name}",
    )

    report = run_doctor(paths, {"PATH": "/usr/bin"})

    check = next(item for item in report.checks if item.name == "tmux:extended_keys")
    assert check.status == "ok"
    assert check.details == {"value": "on", "format": "csi-u"}


def test_private_paths_are_created(tmp_path: Path) -> None:
    paths = RuntimePaths.discover({}, home=tmp_path)
    report = run_doctor(paths, {"PATH": ""})
    path_check = next(item for item in report.checks if item.name == "runtime_paths")
    assert path_check.status == "ok"
    assert paths.config_dir.stat().st_mode & 0o777 == 0o700
