from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone, tzinfo
from types import MappingProxyType

import pytest

from tmuxbot.control_plane.models import RunEvent, SessionClass, TaskState


class _PseudoTimezone(tzinfo):
    def utcoffset(self, dt):
        return None


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


@pytest.mark.parametrize(
    "occurred_at",
    [datetime(2026, 7, 11), datetime(2026, 7, 11, tzinfo=_PseudoTimezone())],
)
def test_run_event_rejects_effectively_naive_timestamp(occurred_at):
    with pytest.raises(ValueError, match="occurred_at must be timezone-aware"):
        RunEvent(
            event_id="evt-naive",
            event_type="session.discovered",
            aggregate_type="session",
            aggregate_id="alpha:0.0",
            payload={},
            occurred_at=occurred_at,
        )


def test_run_event_defensively_copies_payload_and_exposes_it_read_only():
    payload = {"classification": "managed"}
    event = RunEvent(
        event_id="evt-copy",
        event_type="session.discovered",
        aggregate_type="session",
        aggregate_id="alpha:0.0",
        payload=payload,
        occurred_at=datetime(2026, 7, 11, tzinfo=timezone(timedelta(hours=8))),
    )

    payload["classification"] = "orphan"

    assert event.payload == {"classification": "managed"}
    assert isinstance(event.payload, MappingProxyType)
    with pytest.raises(TypeError):
        event.payload["classification"] = "ignored"  # type: ignore[index]
