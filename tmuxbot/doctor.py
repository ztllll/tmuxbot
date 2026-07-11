from __future__ import annotations

import json
import shutil
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Literal

from tmuxbot.paths import RuntimePaths
from tmuxbot.supervisor import inspect_bridge_readiness


@dataclass(frozen=True, slots=True)
class CheckResult:
    name: str
    status: Literal["ok", "warning", "error"]
    summary: str
    details: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class DoctorReport:
    checks: tuple[CheckResult, ...]
    exit_code: int


def _binary_check(name: str, environ: Mapping[str, str]) -> CheckResult:
    path = shutil.which(name, path=environ.get("PATH"))
    if path is None:
        status: Literal["ok", "warning", "error"] = (
            "error" if name == "tmux" else "warning"
        )
        return CheckResult(f"provider:{name}", status, "未发现", {})
    try:
        completed = subprocess.run(
            [path, "-V" if name == "tmux" else "--version"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
            env={"PATH": environ.get("PATH", "")},
        )
        output = (completed.stdout or completed.stderr).strip()[:1024]
    except (OSError, subprocess.SubprocessError) as exc:
        return CheckResult(
            f"provider:{name}", "warning", "版本探测失败", {"error": type(exc).__name__}
        )
    status = "ok" if completed.returncode == 0 else "warning"
    return CheckResult(
        f"provider:{name}", status, "可用" if status == "ok" else "返回异常", {
            "path": path,
            "version": output,
            "returncode": completed.returncode,
        }
    )


def run_doctor(paths: RuntimePaths, environ: Mapping[str, str]) -> DoctorReport:
    checks: list[CheckResult] = []
    py_ok = sys.version_info >= (3, 10)
    checks.append(
        CheckResult(
            "python",
            "ok" if py_ok else "error",
            f"Python {sys.version_info.major}.{sys.version_info.minor}",
            {},
        )
    )
    try:
        paths.ensure_private_directories()
        checks.append(CheckResult("runtime_paths", "ok", "私有目录可用", {}))
    except OSError as exc:
        checks.append(
            CheckResult("runtime_paths", "error", "目录不可用", {"error": type(exc).__name__})
        )
    for name in ("tmux", "claude", "codex"):
        checks.append(_binary_check(name, environ))
    readiness = inspect_bridge_readiness(paths, environ)
    checks.append(
        CheckResult(
            "bridge",
            "ok" if readiness.runnable else "warning",
            readiness.reason,
            {
                "binding_count": readiness.binding_count,
                "frontend_count": readiness.frontend_count,
            },
        )
    )
    exit_code = 1 if any(item.status == "error" for item in checks) else 0
    return DoctorReport(tuple(checks), exit_code)


def render_report(report: DoctorReport, *, as_json: bool) -> str:
    if as_json:
        return json.dumps(
            {
                "schema_version": 1,
                "exit_code": report.exit_code,
                "checks": [asdict(item) for item in report.checks],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    labels = {"ok": "正常", "warning": "提醒", "error": "错误"}
    return "\n".join(
        f"[{labels[item.status]}] {item.name}: {item.summary}" for item in report.checks
    )

