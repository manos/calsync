import dataclasses
import inspect
from datetime import UTC, datetime, timedelta

import pytest

from calsync.model import CalEvent, Marker
from calsync.providers.base import Provider, ProviderError, TransientError
from calsync.providers.fake import FakeProvider

WINDOW = (datetime(2026, 5, 1, tzinfo=UTC), datetime(2026, 5, 8, tzinfo=UTC))
MARKER = Marker("s1", "a" * 16, "h1")
PROTOCOL_METHODS = ("list_events", "list_mirrors", "create", "update", "delete")


def event(uid: str, day: int, marker: Marker | None = None) -> CalEvent:
    return CalEvent(
        uid=uid,
        start=datetime(2026, 5, day, 9, 0, tzinfo=UTC),
        end=datetime(2026, 5, day, 10, 0, tzinfo=UTC),
        title="Thing",
        marker=marker,
    )


def spanning(uid: str, start: datetime, end: datetime) -> CalEvent:
    return CalEvent(uid=uid, start=start, end=end, title="Thing")


def test_list_events_returns_only_events_overlapping_the_window():
    provider = FakeProvider([event("in", 2), event("out", 20)])
    assert [e.uid for e in provider.list_events(WINDOW)] == ["in"]


def test_list_events_treats_the_window_as_half_open():
    start, end = WINDOW
    long_before = start - timedelta(days=5)
    provider = FakeProvider(
        [
            spanning("ends-at-start", start - timedelta(hours=1), start),
            spanning("starts-at-end", end, end + timedelta(hours=1)),
            spanning("entirely-before", long_before, long_before + timedelta(hours=1)),
            spanning("starts-at-start", start, start + timedelta(hours=1)),
        ]
    )
    assert [e.uid for e in provider.list_events(WINDOW)] == ["starts-at-start"]


def test_list_events_is_ordered_by_start_then_uid():
    provider = FakeProvider([event("c", 5), event("b", 2), event("a", 2)])
    assert [e.uid for e in provider.list_events(WINDOW)] == ["a", "b", "c"]


def test_reads_return_copies_so_callers_cannot_mutate_the_store():
    stored = dataclasses.replace(event("x", 2, MARKER), raw={"href": "/x.ics"})
    provider = FakeProvider([stored])

    (returned,) = provider.list_events(WINDOW)
    returned.raw["href"] = "/tampered.ics"

    assert returned is not stored
    assert provider.list_events(WINDOW)[0].raw == {"href": "/x.ics"}


def test_list_mirrors_filters_by_sync_id():
    provider = FakeProvider(
        [
            event("plain", 2),
            event("mine", 2, Marker("s1", "a" * 16, "h1")),
            event("theirs", 2, Marker("s2", "b" * 16, "h2")),
        ]
    )
    assert [e.uid for e in provider.list_mirrors(WINDOW, "s1")] == ["mine"]


def test_list_mirrors_respects_the_window():
    provider = FakeProvider([event("inside", 2, MARKER), event("outside", 20, MARKER)])
    assert [e.uid for e in provider.list_mirrors(WINDOW, "s1")] == ["inside"]


def test_create_update_delete_round_trip():
    provider = FakeProvider()
    original = event("new", 2, MARKER)
    provider.create(original)
    assert [e.uid for e in provider.list_events(WINDOW)] == ["new"]

    provider.update(dataclasses.replace(original, title="Renamed"))
    assert provider.list_events(WINDOW)[0].title == "Renamed"

    provider.delete(original)
    assert provider.list_events(WINDOW) == []


def test_creating_a_duplicate_uid_is_an_error():
    provider = FakeProvider([event("dup", 2, MARKER)])
    with pytest.raises(ProviderError, match="already exists"):
        provider.create(event("dup", 2, MARKER))


def test_updating_a_missing_event_is_an_error():
    provider = FakeProvider()
    with pytest.raises(ProviderError, match="not found"):
        provider.update(event("ghost", 2, MARKER))


def test_deleting_a_missing_event_is_a_no_op():
    provider = FakeProvider([event("keep", 2, MARKER)])
    provider.delete(event("ghost", 2, MARKER))
    assert [e.uid for e in provider.list_events(WINDOW)] == ["keep"]
    assert provider.calls == [("delete", "ghost")]


def test_writing_an_event_without_a_marker_is_refused():
    provider = FakeProvider([event("known", 2, MARKER)])
    with pytest.raises(ProviderError, match="without a calsync marker"):
        provider.create(event("new", 2))
    with pytest.raises(ProviderError, match="without a calsync marker"):
        provider.update(event("known", 2))
    assert [e.uid for e in provider.list_events(WINDOW)] == ["known"]


def test_assign_uid_lets_the_backend_choose_the_stored_uid():
    provider = FakeProvider(assign_uid=lambda e: f"srv-{e.uid}")
    provider.create(event("local", 2, MARKER))

    (stored,) = provider.list_events(WINDOW)
    assert stored.uid == "srv-local"
    assert provider.calls == [("create", "srv-local")]

    provider.delete(stored)
    assert provider.list_events(WINDOW) == []


def test_uid_is_preserved_when_no_assign_uid_is_given():
    provider = FakeProvider()
    provider.create(event("local", 2, MARKER))
    assert [e.uid for e in provider.list_events(WINDOW)] == ["local"]


def test_name_defaults_to_fake_and_is_stored():
    assert FakeProvider().name == "fake"
    assert FakeProvider(name="dest").name == "dest"


def test_calls_are_recorded_for_assertions():
    provider = FakeProvider()
    provider.create(event("a", 2, MARKER))
    provider.update(event("a", 2, MARKER))
    provider.delete(event("a", 2, MARKER))
    assert provider.calls == [("create", "a"), ("update", "a"), ("delete", "a")]


def test_transient_error_is_a_provider_error():
    assert issubclass(TransientError, ProviderError)


def test_fake_satisfies_the_provider_protocol():
    assert isinstance(FakeProvider(), Provider)


@pytest.mark.parametrize("method", PROTOCOL_METHODS)
def test_fake_matches_the_protocol_signatures(method: str):
    assert inspect.signature(getattr(FakeProvider, method)) == inspect.signature(
        getattr(Provider, method)
    )
