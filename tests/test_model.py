import dataclasses
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest

from calsync.model import CalEvent, Marker


@pytest.mark.parametrize("naive_field", ["start", "end"])
def test_event_requires_aware_datetimes(naive_field):
    times = {
        "start": datetime(2026, 5, 1, 9, 0, tzinfo=UTC),
        "end": datetime(2026, 5, 1, 10, 0, tzinfo=UTC),
    }
    times[naive_field] = times[naive_field].replace(tzinfo=None)
    with pytest.raises(ValueError, match="timezone-aware") as excinfo:
        CalEvent(uid="evt-42", **times)
    message = str(excinfo.value)
    assert naive_field in message
    assert "evt-42" in message


def test_event_rejects_dates():
    with pytest.raises(ValueError, match="must be a datetime, got date"):
        CalEvent(
            uid="a",
            start=date(2026, 5, 1),
            end=datetime(2026, 5, 1, 10, 0, tzinfo=UTC),
        )


def test_event_normalizes_to_utc():
    event = CalEvent(
        uid="a",
        start=datetime(2026, 5, 1, 9, 0, tzinfo=ZoneInfo("America/New_York")),
        end=datetime(2026, 5, 1, 10, 0, tzinfo=ZoneInfo("America/New_York")),
    )
    assert event.start == datetime(2026, 5, 1, 13, 0, tzinfo=UTC)
    assert event.start.tzinfo is UTC
    assert event.end == datetime(2026, 5, 1, 14, 0, tzinfo=UTC)
    assert event.end.tzinfo is UTC


def test_event_rejects_end_before_start():
    with pytest.raises(ValueError, match="end before start") as excinfo:
        CalEvent(
            uid="evt-42",
            start=datetime(2026, 5, 1, 10, 0, tzinfo=UTC),
            end=datetime(2026, 5, 1, 9, 0, tzinfo=UTC),
        )
    message = str(excinfo.value)
    assert "evt-42" in message
    assert "2026-05-01T10:00" in message
    assert "2026-05-01T09:00" in message


def test_event_is_frozen():
    event = CalEvent(
        uid="a",
        start=datetime(2026, 5, 1, 9, 0, tzinfo=UTC),
        end=datetime(2026, 5, 1, 10, 0, tzinfo=UTC),
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        event.title = "changed"


def test_raw_is_excluded_from_equality():
    times = {
        "start": datetime(2026, 5, 1, 9, 0, tzinfo=UTC),
        "end": datetime(2026, 5, 1, 10, 0, tzinfo=UTC),
    }
    bare = CalEvent(uid="a", **times)
    addressed = CalEvent(uid="a", raw={"href": "/cal/a.ics"}, **times)
    assert bare == addressed
    assert hash(bare) == hash(addressed)


def test_is_mirror_reflects_marker():
    plain = CalEvent(
        uid="a",
        start=datetime(2026, 5, 1, 9, 0, tzinfo=UTC),
        end=datetime(2026, 5, 1, 10, 0, tzinfo=UTC),
    )
    assert plain.is_mirror is False
    mirrored = CalEvent(
        uid="b",
        start=plain.start,
        end=plain.end,
        marker=Marker(sync_id="s", key="0123456789abcdef", hash="deadbeef"),
    )
    assert mirrored.is_mirror is True
