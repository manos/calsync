import dataclasses
import logging
from datetime import UTC, datetime

import pytest

from calsync.config import load_config
from calsync.model import CalEvent
from calsync.providers.base import ProviderError, Window
from calsync.providers.fake import FakeProvider
from calsync.sync import DeleteRailTripped, by_key, run_sync

NOW = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)

CONFIG = """
accounts:
  a: {type: google, client_id: c, client_secret: s, refresh_token: r}
  b: {type: google, client_id: c, client_secret: s, refresh_token: r}
defaults:
  max_deletes_per_pass: 2
syncs:
  - id: s1
    source: {account: a, calendar: primary}
    dest: {account: b, calendar: primary}
    padding: {before: 15m, after: 15m}
"""


def spec():
    return load_config(CONFIG, env={}).syncs[0]


def source_event(uid: str, day: int = 2, **kwargs) -> CalEvent:
    base = {
        "uid": uid,
        "start": datetime(2026, 5, day, 9, 0, tzinfo=UTC),
        "end": datetime(2026, 5, day, 10, 0, tzinfo=UTC),
        "title": "Dentist",
    }
    return CalEvent(**{**base, **kwargs})


def test_new_source_events_are_created_on_the_destination():
    source = FakeProvider([source_event("a")])
    dest = FakeProvider()
    result = run_sync(spec(), source, dest, now=NOW)

    assert result.created == 1
    (mirror,) = dest.list_events(spec().window.bounds(NOW))
    assert mirror.title == "Busy"
    assert mirror.start == datetime(2026, 5, 2, 8, 45, tzinfo=UTC)


def test_a_second_pass_over_unchanged_input_does_nothing():
    source = FakeProvider([source_event("a")])
    dest = FakeProvider()
    run_sync(spec(), source, dest, now=NOW)
    dest.calls.clear()

    result = run_sync(spec(), source, dest, now=NOW)
    assert (result.created, result.updated, result.deleted) == (0, 0, 0)
    assert dest.calls == []


def test_a_moved_source_event_updates_the_existing_mirror_in_place():
    original = source_event("a")
    source = FakeProvider([original])
    # The destination assigns its own native uid on create, as Google does, so
    # addressing the update by the *desired* uid would fail loudly here.
    dest = FakeProvider(assign_uid=lambda event: "gid-" + event.marker.key)
    run_sync(spec(), source, dest, now=NOW)
    (before,) = dest.list_events(spec().window.bounds(NOW))

    source.events["a"] = dataclasses.replace(original, end=datetime(2026, 5, 2, 11, 0, tzinfo=UTC))
    result = run_sync(spec(), source, dest, now=NOW)

    assert (result.created, result.updated, result.deleted) == (0, 1, 0)
    (after,) = dest.list_events(spec().window.bounds(NOW))
    assert after.uid == before.uid
    assert after.end == datetime(2026, 5, 2, 11, 15, tzinfo=UTC)


def test_update_carries_backend_addressing_detail_from_the_existing_mirror():
    original = source_event("a")
    source = FakeProvider([original])
    dest = FakeProvider()
    run_sync(spec(), source, dest, now=NOW)

    stored = next(iter(dest.events.values()))
    dest.events[stored.uid] = dataclasses.replace(stored, raw={"href": "/cal/abc.ics"})
    source.events["a"] = dataclasses.replace(original, end=datetime(2026, 5, 2, 11, 0, tzinfo=UTC))
    run_sync(spec(), source, dest, now=NOW)

    assert next(iter(dest.events.values())).raw == {"href": "/cal/abc.ics"}


def test_a_deleted_source_event_removes_its_mirror():
    source = FakeProvider([source_event("a")])
    dest = FakeProvider()
    run_sync(spec(), source, dest, now=NOW)

    source.events.clear()
    result = run_sync(spec(), source, dest, now=NOW)

    assert result.deleted == 1
    assert dest.list_events(spec().window.bounds(NOW)) == []


def test_mirrors_belonging_to_another_sync_are_untouched():
    source = FakeProvider()
    dest = FakeProvider()
    other = spec().model_copy(update={"id": "s2"})
    run_sync(other, FakeProvider([source_event("a")]), dest, now=NOW)
    dest.calls.clear()

    result = run_sync(spec(), source, dest, now=NOW)
    assert (result.created, result.updated, result.deleted) == (0, 0, 0)
    assert len(dest.events) == 1


def test_filtered_source_events_are_counted_as_skipped():
    source = FakeProvider([source_event("a"), source_event("b", declined=True)])
    dest = FakeProvider()
    result = run_sync(spec(), source, dest, now=NOW)
    assert (result.created, result.skipped) == (1, 1)


def test_delete_rail_aborts_the_sync_without_applying_anything():
    source = FakeProvider([source_event(uid, day) for uid, day in [("a", 2), ("b", 3), ("c", 4)]])
    dest = FakeProvider()
    run_sync(spec(), source, dest, now=NOW)
    # A pending create as well, so this pins the rail's headline property: when
    # tripped, *nothing* is applied, not even the operations that only add.
    source.events.clear()
    source.events["d"] = source_event("d", day=5)
    dest.calls.clear()

    with pytest.raises(DeleteRailTripped, match="3 deletions"):
        run_sync(spec(), source, dest, now=NOW)
    assert dest.calls == []
    assert len(dest.events) == 3


def test_deleting_exactly_the_rail_limit_is_allowed():
    source = FakeProvider([source_event("a", 2), source_event("b", 3)])
    dest = FakeProvider()
    run_sync(spec(), source, dest, now=NOW)
    source.events.clear()

    result = run_sync(spec(), source, dest, now=NOW)

    assert result.deleted == spec().max_deletes_per_pass == 2
    assert dest.list_events(spec().window.bounds(NOW)) == []


def test_a_mirror_with_no_stored_hash_is_updated():
    source = FakeProvider([source_event("a")])
    dest = FakeProvider()
    run_sync(spec(), source, dest, now=NOW)

    # A backend that dropped the stored hash leaves the mirror's content unknown.
    stored = next(iter(dest.events.values()))
    dest.events[stored.uid] = dataclasses.replace(
        stored, marker=dataclasses.replace(stored.marker, hash="")
    )
    result = run_sync(spec(), source, dest, now=NOW)

    assert (result.created, result.updated, result.deleted) == (0, 1, 0)


def test_a_rekeyed_event_is_created_before_its_old_mirror_is_deleted():
    original = source_event("a")
    source = FakeProvider([original])
    dest = FakeProvider()
    run_sync(spec(), source, dest, now=NOW)
    dest.calls.clear()

    # Moving the start changes the calsync key, so the pass is a create plus a
    # delete; the user must never be shown briefly free in between.
    source.events["a"] = dataclasses.replace(
        original,
        start=datetime(2026, 5, 3, 9, 0, tzinfo=UTC),
        end=datetime(2026, 5, 3, 10, 0, tzinfo=UTC),
    )
    result = run_sync(spec(), source, dest, now=NOW)

    assert (result.created, result.deleted) == (1, 1)
    assert [kind for kind, _ in dest.calls] == ["create", "delete"]


def test_by_key_ignores_events_without_a_marker():
    assert by_key([source_event("a")], "s1") == {}


def test_dry_run_reports_operations_without_touching_the_destination():
    source = FakeProvider([source_event("a")])
    dest = FakeProvider()
    result = run_sync(spec(), source, dest, now=NOW, dry_run=True)

    assert result.created == 1
    assert result.dry_run is True
    assert dest.calls == []
    assert dest.events == {}


def test_events_outside_the_window_are_ignored():
    source = FakeProvider([source_event("far", day=30)])
    dest = FakeProvider()
    result = run_sync(spec(), source, dest, now=NOW)
    assert result.created == 0


def test_a_duplicate_mirror_sharing_a_key_is_reaped():
    source = FakeProvider([source_event("a")])
    dest = FakeProvider()
    run_sync(spec(), source, dest, now=NOW)

    # What a provider retry after a 503 that actually committed leaves behind:
    # two mirrors carrying the same calsync key.
    stored = next(iter(dest.events.values()))
    dest.events["dup"] = dataclasses.replace(stored, uid="dup")
    dest.calls.clear()

    result = run_sync(spec(), source, dest, now=NOW)

    assert (result.created, result.updated, result.deleted) == (0, 0, 1)
    (survivor,) = dest.list_events(spec().window.bounds(NOW))
    assert survivor.marker.key == stored.marker.key


class OverReturningProvider(FakeProvider):
    """A provider whose ``list_mirrors`` ignores the sync_id it was given."""

    def list_mirrors(self, window: Window, sync_id: str) -> list[CalEvent]:
        return [event for event in self.list_events(window) if event.is_mirror]


def test_another_syncs_mirror_survives_a_provider_that_over_returns():
    dest = OverReturningProvider()
    other = spec().model_copy(update={"id": "s2"})
    run_sync(other, FakeProvider([source_event("a")]), dest, now=NOW)
    dest.calls.clear()

    result = run_sync(spec(), FakeProvider(), dest, now=NOW)

    assert (result.created, result.updated, result.deleted) == (0, 0, 0)
    assert dest.calls == []
    assert len(dest.events) == 1


class DoubleListingProvider(FakeProvider):
    """A provider that hands back the same occurrence twice, as backends do."""

    def list_events(self, window: Window) -> list[CalEvent]:
        events = super().list_events(window)
        return events + events


def test_a_duplicate_source_occurrence_is_not_counted_as_skipped():
    source = DoubleListingProvider([source_event("a")])
    dest = FakeProvider()

    result = run_sync(spec(), source, dest, now=NOW)

    assert (result.created, result.skipped) == (1, 0)


class FailingDeleteProvider(FakeProvider):
    def delete(self, event: CalEvent) -> None:
        raise ProviderError("backend exploded")


def test_a_failure_part_way_through_apply_logs_what_was_already_applied(caplog):
    original = source_event("a")
    source = FakeProvider([original])
    dest = FailingDeleteProvider()
    run_sync(spec(), source, dest, now=NOW)
    source.events["a"] = dataclasses.replace(
        original,
        start=datetime(2026, 5, 3, 9, 0, tzinfo=UTC),
        end=datetime(2026, 5, 3, 10, 0, tzinfo=UTC),
    )

    with caplog.at_level(logging.ERROR, logger="calsync.sync"):
        with pytest.raises(ProviderError):
            run_sync(spec(), source, dest, now=NOW)

    assert "created=1" in caplog.text
    assert "deleted=0" in caplog.text
