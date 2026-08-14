import shutil
import zipfile

import pytest

from tmuxbot import omp_extension


def test_wheel_contains_managed_omp_handoff_extension(tmp_path):
    import subprocess

    uv_bin = shutil.which("uv")
    if uv_bin is None:
        pytest.skip("uv executable is required for wheel content test")
    subprocess.run(
        [uv_bin, "build", "--wheel", "--out-dir", str(tmp_path)],
        check=True,
        capture_output=True,
        text=True,
    )
    wheel = next(tmp_path.glob("tmuxbot-*.whl"))

    with zipfile.ZipFile(wheel) as archive:
        assert "tmuxbot/omp-extensions/tmuxbot-session-handoff.ts" in archive.namelist()


def test_managed_omp_extension_source_is_existing_absolute_packaged_path():
    source = omp_extension.managed_extension_source()

    assert source.is_absolute()
    assert source.is_file()
    assert source.name == "tmuxbot-session-handoff.ts"
    assert ".omp/agent/extensions" not in str(source)


def test_managed_omp_extension_uses_public_identity_and_agent_lifecycle_events():
    source = omp_extension.managed_extension_source().read_text(encoding="utf-8")

    assert 'from "@oh-my-pi/pi-coding-agent"' in source
    assert 'omp.on("session_start"' in source
    assert 'omp.on("session_switch"' in source
    assert 'omp.on("agent_start"' in source
    assert 'omp.on("message_end"' in source
    assert 'omp.on("agent_end"' in source
    assert "willContinue" in source
    assert "isTerminal" in source
    assert "agent_settled" not in source
    assert "await refreshIdentity(ctx)" in source
    assert "processId: process.pid" in source
