import json
import stat

import pytest

from tmuxbot.core.im_delivery_audit import increment, save, snapshot


def test_im_delivery_audit_records_only_counts_and_body_length(tmp_path):
    registry = {}
    increment(registry, "alpha", "progress_created")
    increment(registry, "alpha", "progress_edited", 2)
    increment(registry, "alpha", "results_published")
    increment(registry, "alpha", "result_body_chars", 42)

    data = snapshot(registry)

    assert data["routes"]["alpha"]["progress_created"] == 1
    assert data["totals"]["progress_edited"] == 2
    assert data["totals"]["result_body_chars"] == 42
    serialized = json.dumps(data)
    assert "secret user content" not in serialized
    assert set(data["routes"]["alpha"]) == set(data["totals"])

    path = tmp_path / "im-delivery.json"
    save(path, registry)
    assert json.loads(path.read_text())["version"] == 1
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_im_delivery_audit_rejects_unknown_counter():
    with pytest.raises(ValueError, match="unknown IM delivery counter"):
        increment({}, "alpha", "raw_message_body")
