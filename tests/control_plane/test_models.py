from dataclasses import FrozenInstanceError
from datetime import datetime, timezone

import pytest

from tmuxbot.control_plane.models import RunEvent, SessionClass, TaskState


def test_run_event_is_immutable_and_uses_utc_timestamp():
    event = RunEvent(
        event_id="evt-1",
        event_type="session.discovered",
        aggregate_type="session",
        aggregate_id="alpha:0.0",
        payload={"classification": "managed"},
        occurred_at=datetime.now(timezone.utc),
    )

    assert event.occurred_at.tzinfo is timezone.utc
    with pytest.raises(FrozenInstanceError):
        event.event_type = "changed"  # type: ignore[misc]


def test_foundation_enums_keep_storage_values_stable():
    assert TaskState.OPERATOR_REQUIRED.value == "operator_required"
    assert SessionClass.ORPHAN.value == "orphan"
