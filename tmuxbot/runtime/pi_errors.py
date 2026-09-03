"""Classify Pi provider error messages that represent explicit user cancellation."""
from __future__ import annotations

import re

_USER_ABORT_MESSAGES = {
    "operation aborted",
    "request was aborted",
    "the operation was aborted",
    "this operation was aborted",
}


def is_user_abort_error(message: object) -> bool:
    """Return true only for Pi's known explicit cancellation messages."""
    normalized = re.sub(r"[.!]+$", "", str(message or "").strip().lower())
    return normalized in _USER_ABORT_MESSAGES
