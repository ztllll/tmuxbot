import os
import threading
import time
from pathlib import Path

import pytest

from tmuxbot.control_plane.models import ProviderProfile
from tmuxbot.providers.discovery import (
    MAX_PROBE_OUTPUT_BYTES,
    ProviderDiscovery,
    ProviderDiscoveryError,
)


def _executable(path: Path, body: str) -> Path:
    path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    path.chmod(0o755)
    return path


def test_scan_only_discovers_allowlisted_regular_executables(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    real_codex = _executable(bin_dir / "codex-real", "printf 'codex 1.0\\n'\n")
    (bin_dir / "codex").symlink_to(real_codex)
    _executable(bin_dir / "claude", "printf 'claude 2.0\\n'\n")
    _executable(bin_dir / "evil", "printf 'evil\\n'\n")
    (bin_dir / "tmux").mkdir()
    monkeypatch.setenv("PATH", str(bin_dir))

    found = ProviderDiscovery().scan()

    assert [item.binary_name for item in found] == ["claude", "codex"]
    codex = next(item for item in found if item.binary_name == "codex")
    assert codex.executable_path == str(real_codex.resolve())
    info = real_codex.stat()
    assert (codex.device, codex.inode, codex.mtime_ns) == (
        info.st_dev,
        info.st_ino,
        info.st_mtime_ns,
    )


def test_probe_uses_exact_argv_without_shell_and_returns_version(tmp_path):
    executable = _executable(
        tmp_path / "codex", "test \"$1\" = --version || exit 9\nprintf 'codex 3.4.5\\n'\n"
    )
    info = executable.stat()
    provider = ProviderProfile(
        id="provider-one",
        binary_name="codex",
        executable_path=str(executable.resolve()),
        version=None,
        device=info.st_dev,
        inode=info.st_ino,
        mtime_ns=info.st_mtime_ns,
        discovered_at=int(time.time()),
    )

    result = ProviderDiscovery().probe(provider)

    assert result.success is True
    assert result.version == "codex 3.4.5"


def test_tmux_probe_uses_native_version_flag(tmp_path):
    executable = _executable(
        tmp_path / "tmux", "test \"$1\" = -V || exit 9\nprintf 'tmux 3.4\n'\n"
    )
    info = executable.stat()
    provider = ProviderProfile(
        id="provider-tmux",
        binary_name="tmux",
        executable_path=str(executable),
        version=None,
        device=info.st_dev,
        inode=info.st_ino,
        mtime_ns=info.st_mtime_ns,
        discovered_at=1,
    )

    result = ProviderDiscovery().probe(provider)

    assert result.success is True
    assert result.version == "tmux 3.4"
    assert result.exit_code == 0
    assert result.error_code is None


def test_probe_only_keeps_single_version_line_and_discards_other_output(tmp_path):
    executable = _executable(
        tmp_path / "claude",
        "printf 'claude 9.1\\nSECRET_TOKEN=must-not-persist\\n'\n"
        "printf 'password=must-not-persist\\n' >&2\n",
    )
    info = executable.stat()
    provider = ProviderProfile(
        id="provider-redacted",
        binary_name="claude",
        executable_path=str(executable),
        version=None,
        device=info.st_dev,
        inode=info.st_ino,
        mtime_ns=info.st_mtime_ns,
        discovered_at=1,
    )

    result = ProviderDiscovery().probe(provider)

    assert result.version == "claude 9.1"
    assert "SECRET_TOKEN" not in repr(result)
    assert "password" not in repr(result)


def test_probe_enforces_output_cap(tmp_path):
    executable = _executable(
        tmp_path / "claude",
        "head -c 70000 /dev/zero | tr '\\000' x\nprintf '\\n'\n",
    )
    info = executable.stat()
    provider = ProviderProfile(
        id="provider-large",
        binary_name="claude",
        executable_path=str(executable),
        version=None,
        device=info.st_dev,
        inode=info.st_ino,
        mtime_ns=info.st_mtime_ns,
        discovered_at=1,
    )

    result = ProviderDiscovery().probe(provider)

    assert result.success is False
    assert result.error_code == "output_too_large"
    assert result.output_truncated is True
    assert result.version is None
    assert MAX_PROBE_OUTPUT_BYTES == 64 * 1024


def test_probe_enforces_timeout(tmp_path):
    executable = _executable(tmp_path / "tmux", "sleep 1\nprintf 'tmux 9\\n'\n")
    info = executable.stat()
    provider = ProviderProfile(
        id="provider-timeout",
        binary_name="tmux",
        executable_path=str(executable),
        version=None,
        device=info.st_dev,
        inode=info.st_ino,
        mtime_ns=info.st_mtime_ns,
        discovered_at=1,
    )

    started = time.monotonic()
    result = ProviderDiscovery(timeout_seconds=0.05).probe(provider)

    assert time.monotonic() - started < 0.5
    assert result.success is False
    assert result.error_code == "timeout"


def test_probe_rejects_toctou_identity_change(tmp_path):
    executable = _executable(tmp_path / "codex", "printf 'codex old\\n'\n")
    info = executable.stat()
    provider = ProviderProfile(
        id="provider-changed",
        binary_name="codex",
        executable_path=str(executable),
        version=None,
        device=info.st_dev,
        inode=info.st_ino,
        mtime_ns=info.st_mtime_ns,
        discovered_at=1,
    )
    replacement = _executable(tmp_path / "replacement", "printf 'codex new\\n'\n")
    os.replace(replacement, executable)

    with pytest.raises(ProviderDiscoveryError, match="identity_changed"):
        ProviderDiscovery().probe(provider)


def test_probe_rejects_identity_replacement_during_command(tmp_path):
    executable = _executable(
        tmp_path / "codex", "sleep 0.15\nprintf 'codex old\\n'\n"
    )
    info = executable.stat()
    provider = ProviderProfile(
        id="provider-raced",
        binary_name="codex",
        executable_path=str(executable),
        version=None,
        device=info.st_dev,
        inode=info.st_ino,
        mtime_ns=info.st_mtime_ns,
        discovered_at=1,
    )

    def replace_while_running():
        time.sleep(0.05)
        replacement = _executable(
            tmp_path / "replacement-during-probe", "printf 'codex new\\n'\n"
        )
        os.replace(replacement, executable)

    racer = threading.Thread(target=replace_while_running)
    racer.start()
    try:
        with pytest.raises(ProviderDiscoveryError, match="identity_changed"):
            ProviderDiscovery(timeout_seconds=0.5).probe(provider)
    finally:
        racer.join()
