from __future__ import annotations

import os
import selectors
import shutil
import signal
import stat
import subprocess
import time
import uuid
from pathlib import Path

from tmuxbot.control_plane.models import (
    PROVIDER_BINARIES,
    ProviderProfile,
    ProviderProbeResult,
)


MAX_PROBE_OUTPUT_BYTES = 64 * 1024
DEFAULT_PROBE_TIMEOUT_SECONDS = 3.0


class ProviderDiscoveryError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class ProviderDiscovery:
    def __init__(self, *, timeout_seconds: float = DEFAULT_PROBE_TIMEOUT_SECONDS) -> None:
        if timeout_seconds <= 0 or timeout_seconds > DEFAULT_PROBE_TIMEOUT_SECONDS:
            raise ValueError("provider probe timeout must be between 0 and 3 seconds")
        self.timeout_seconds = timeout_seconds

    def scan(self) -> list[ProviderProfile]:
        discovered: list[ProviderProfile] = []
        now = int(time.time())
        for binary_name in sorted(PROVIDER_BINARIES):
            candidate = shutil.which(binary_name)
            if not candidate:
                continue
            try:
                executable = Path(os.path.realpath(candidate))
                info = executable.stat(follow_symlinks=False)
            except OSError:
                continue
            if not stat.S_ISREG(info.st_mode) or not os.access(executable, os.X_OK):
                continue
            discovered.append(
                ProviderProfile(
                    id=f"provider-{uuid.uuid4().hex}",
                    binary_name=binary_name,
                    executable_path=str(executable),
                    version=None,
                    device=info.st_dev,
                    inode=info.st_ino,
                    mtime_ns=info.st_mtime_ns,
                    discovered_at=now,
                )
            )
        return discovered

    def probe(self, provider: ProviderProfile) -> ProviderProbeResult:
        self._verify_identity(provider)
        started = time.monotonic()
        stdout, _stderr, exit_code, truncated, timed_out, unavailable = _run_bounded(
            [provider.executable_path, "--version"],
            timeout_seconds=self.timeout_seconds,
        )
        self._verify_identity(provider)
        duration_ms = max(0, int((time.monotonic() - started) * 1000))
        if timed_out:
            success = False
            error_code = "timeout"
            version = None
        elif unavailable:
            success = False
            error_code = "unavailable"
            version = None
        elif truncated:
            success = False
            error_code = "output_too_large"
            version = None
        elif exit_code != 0:
            success = False
            error_code = "command_failed"
            version = None
        else:
            version = _extract_version(stdout)
            success = bool(version)
            error_code = None if success else "empty_output"
        return ProviderProbeResult(
            id=f"probe-{uuid.uuid4().hex}",
            provider_id=provider.id,
            success=success,
            version=version,
            error_code=error_code,
            exit_code=exit_code,
            duration_ms=duration_ms,
            output_truncated=truncated,
            observed_at=int(time.time()),
        )

    @staticmethod
    def _verify_identity(provider: ProviderProfile) -> None:
        if provider.binary_name not in PROVIDER_BINARIES:
            raise ProviderDiscoveryError("not_allowlisted")
        if os.path.realpath(provider.executable_path) != provider.executable_path:
            raise ProviderDiscoveryError("identity_changed")
        try:
            info = os.stat(provider.executable_path, follow_symlinks=False)
        except OSError as exc:
            raise ProviderDiscoveryError("identity_changed") from exc
        identity = (info.st_dev, info.st_ino, info.st_mtime_ns)
        expected = (provider.device, provider.inode, provider.mtime_ns)
        if (
            not stat.S_ISREG(info.st_mode)
            or not os.access(provider.executable_path, os.X_OK)
            or identity != expected
        ):
            raise ProviderDiscoveryError("identity_changed")


def _run_bounded(
    argv: list[str], *, timeout_seconds: float
) -> tuple[bytes, bytes, int | None, bool, bool, bool]:
    try:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            close_fds=True,
            start_new_session=True,
        )
    except (OSError, ValueError):
        return b"", b"", None, False, False, True

    assert process.stdout is not None and process.stderr is not None
    selector = selectors.DefaultSelector()
    buffers = {process.stdout: bytearray(), process.stderr: bytearray()}
    seen = 0
    truncated = False
    timed_out = False
    try:
        for stream in buffers:
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ)
        deadline = time.monotonic() + timeout_seconds
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                _terminate_process_group(process)
                break
            events = selector.select(timeout=remaining)
            if not events and process.poll() is None:
                continue
            for key, _mask in events:
                stream = key.fileobj
                try:
                    chunk = os.read(stream.fileno(), 8192)
                except BlockingIOError:
                    continue
                if not chunk:
                    selector.unregister(stream)
                    continue
                room = max(0, MAX_PROBE_OUTPUT_BYTES - seen)
                buffers[stream].extend(chunk[:room])
                seen += len(chunk)
                if seen > MAX_PROBE_OUTPUT_BYTES:
                    truncated = True
        if process.poll() is None:
            process.wait(timeout=0.2)
    except subprocess.TimeoutExpired:
        timed_out = True
        _terminate_process_group(process)
    finally:
        selector.close()
        for stream in buffers:
            stream.close()
        if process.poll() is None:
            _terminate_process_group(process)
        process.wait()
    return (
        bytes(buffers[process.stdout]),
        bytes(buffers[process.stderr]),
        process.returncode,
        truncated,
        timed_out,
        False,
    )


def _extract_version(stdout: bytes) -> str | None:
    for line in stdout.decode("utf-8", errors="replace").splitlines():
        candidate = line.strip()
        if not candidate:
            continue
        if len(candidate.encode("utf-8")) > 512:
            return None
        return candidate
    return None


def _terminate_process_group(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except OSError:
        process.kill()
