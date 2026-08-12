import shutil
import zipfile

import pytest

from tmuxbot import pi_extension


def test_wheel_contains_managed_pi_handoff_extension(tmp_path):
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
        assert (
            "tmuxbot/pi-extensions/tmuxbot-session-handoff.ts"
            in archive.namelist()
        )


def test_install_pi_handoff_extension_is_private_and_idempotent(tmp_path, monkeypatch):
    source = tmp_path / "source.ts"
    source.write_text("export default () => {};\n", encoding="utf-8")
    monkeypatch.setattr(pi_extension, "managed_extension_source", lambda: source)
    monkeypatch.setattr(pi_extension.Path, "home", lambda: tmp_path / "home")

    installed = pi_extension.install_pi_handoff_extension()

    assert installed.read_text(encoding="utf-8") == source.read_text(encoding="utf-8")
    assert installed.stat().st_mode & 0o777 == 0o600
    assert pi_extension.install_pi_handoff_extension() == installed
