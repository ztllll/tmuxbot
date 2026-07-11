from __future__ import annotations

import email
import zipfile
from pathlib import Path


def test_built_wheel_contains_full_runtime_and_web_assets(tmp_path: Path) -> None:
    import subprocess

    subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(tmp_path)],
        check=True,
        capture_output=True,
        text=True,
    )
    wheel = next(tmp_path.glob("tmuxbot-*.whl"))
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
        metadata_name = next(name for name in names if name.endswith(".dist-info/METADATA"))
        metadata = email.message_from_bytes(archive.read(metadata_name))
    assert "full" in metadata.get_all("Provides-Extra", [])
    assert "tmuxbot/web/static/index.html" in names
    assert any(name.startswith("tmuxbot/web/static/assets/") for name in names)
    assert not any(name.endswith(("/.env", "/bindings.yaml", ".sqlite3")) for name in names)
