"""Fetch Claude Pro/Max subscription quota from /api/oauth/usage.

claude TUI 自己 /status 也是打这个端点拿配额数据 — 我们走同样的 OAuth bearer
token (``~/.claude/.credentials.json``) 拿到 5h / 7d (总 / Opus / Sonnet) 等多个
配额窗口的 utilization + resets_at,远比从屏幕 regex parse /cost 输出更准。

借鉴自 bot-im-channel/quota.py(感谢自己上个项目),纯 stdlib 实现 + 30s cache
防止 spam API。token 拿不到或网络挂了 → 返回 None,/status 走兜底文案。

Response shape::

    {
      "five_hour":         {"utilization": 3.0,  "resets_at": "2026-05-18T13:00:00+00:00"},
      "seven_day":         {"utilization": 50.0, "resets_at": "2026-05-22T23:00:00+00:00"},
      "seven_day_sonnet":  {"utilization": 27.0, "resets_at": "..."},
      "seven_day_opus":    null,
      "extra_usage":       {"is_enabled": false, ...}
    }
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

log = logging.getLogger(__name__)

_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
_CACHE_TTL_SECONDS = 30.0
_FETCH_TIMEOUT = 6.0


class _QuotaCache:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._payload: dict | None = None
        self._fetched_at: float = 0.0
        self._last_error: str | None = None

    def get(self, *, force: bool = False) -> tuple[dict | None, float, str | None]:
        now = time.time()
        with self._lock:
            if (
                not force
                and self._payload is not None
                and now - self._fetched_at < _CACHE_TTL_SECONDS
            ):
                return self._payload, self._fetched_at, self._last_error
        payload, err = _do_fetch()
        with self._lock:
            if payload is not None:
                self._payload = payload
                self._fetched_at = time.time()
                self._last_error = None
            else:
                self._last_error = err
            return self._payload, self._fetched_at, self._last_error


_cache = _QuotaCache()


def _read_token() -> str | None:
    p = Path("~/.claude/.credentials.json").expanduser()
    if not p.is_file():
        return None
    try:
        d = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return (d.get("claudeAiOauth") or {}).get("accessToken")


def _do_fetch() -> tuple[dict | None, str | None]:
    token = _read_token()
    if not token:
        return None, "no OAuth token in ~/.claude/.credentials.json"
    req = urllib.request.Request(
        _USAGE_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": "oauth-2025-04-20",
            "User-Agent": "tmuxbot/quota.py",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=_FETCH_TIMEOUT) as resp:
            body = resp.read()
        payload = json.loads(body)
        if not isinstance(payload, dict):
            return None, f"unexpected response shape: {type(payload).__name__}"
        return payload, None
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code} from {_USAGE_URL}"
    except (urllib.error.URLError, TimeoutError) as e:
        return None, f"network: {e}"
    except (json.JSONDecodeError, OSError) as e:
        return None, f"parse: {e}"


def fetch_quota(*, force: bool = False) -> tuple[dict | None, float, str | None]:
    """Return (payload, fetched_at, last_error).

    ``payload`` is the parsed JSON dict on success, or None on failure.
    ``fetched_at`` is the unix time of the last *successful* fetch (0 if never).
    ``last_error`` carries the most recent failure reason.
    """
    return _cache.get(force=force)
