"""The scenarios the project exists for, driven end to end through ``main``.

These are regression guards, not design tests: the implementation is already in
place, so a failure here is a real defect in the source, never an assertion to
be relaxed.
"""

import dataclasses
from datetime import UTC, datetime, timedelta

from calsync.cli import main
from calsync.model import CalEvent
from calsync.providers.fake import FakeProvider

CONFIG = """
accounts:
  icloud: {type: caldav, url: https://caldav.icloud.com, username: me@x.com, password: pw}
  work: {type: google, client_id: c, client_secret: s, refresh_token: r}
  shared: {type: google, client_id: c, client_secret: s, refresh_token: r}

defaults:
  padding: {before: 15m, after: 15m}

syncs:
  - id: personal-to-work
    source: {account: icloud, calendar: Home}
    dest: {account: work, calendar: primary}
  - id: work-to-shared
    source: {account: work, calendar: primary}
    dest: {account: shared, calendar: Shared}
    privacy: full
    padding: {before: 0m, after: 0m}
"""

# Two syncs from different sources landing in one destination calendar, the
# arrangement where one sync could reap the other's mirrors.
CONFIG_TWO_INTO_ONE = """
accounts:
  icloud: {type: caldav, url: https://caldav.icloud.com, username: me@x.com, password: pw}
  work: {type: google, client_id: c, client_secret: s, refresh_token: r}
  shared: {type: google, client_id: c, client_secret: s, refresh_token: r}

syncs:
  - id: personal-to-shared
    source: {account: icloud, calendar: Home}
    dest: {account: shared, calendar: Shared}
  - id: work-to-shared
    source: {account: work, calendar: primary}
    dest: {account: shared, calendar: Shared}
"""

# One instant for the whole module. Recomputing ``datetime.now`` per call would
# make an event's start and the assertion about it differ by a second whenever
# the clock ticks between them, turning these into occasional false failures.
NOW = datetime.now(UTC).replace(microsecond=0)


def soon(hours: int) -> datetime:
    return NOW + timedelta(hours=hours)


def midnight(days: int) -> datetime:
    return NOW.replace(hour=0, minute=0, second=0) + timedelta(days=days)


def event(uid: str, title: str, hours: int, **kwargs) -> CalEvent:
    fields = {"description": "private notes", "location": "home", **kwargs}
    return CalEvent(uid=uid, start=soon(hours), end=soon(hours + 1), title=title, **fields)


def run(tmp_path, providers, config: str = CONFIG) -> int:
    path = tmp_path / "config.yaml"
    path.write_text(config)
    return main(
        ["sync", "--once", "--config", str(path)],
        factory=lambda name, account, calendar: providers[name],
    )


def mirrors(provider: FakeProvider) -> list[CalEvent]:
    found = [e for e in provider.events.values() if e.is_mirror]
    return sorted(found, key=lambda e: e.marker.key)


def test_personal_detail_never_reaches_work_and_work_detail_reaches_shared(tmp_path):
    icloud = FakeProvider([event("therapy", "Therapy", 24)])
    work = FakeProvider([event("standup", "Standup", 26)])
    shared = FakeProvider()
    providers = {"icloud": icloud, "work": work, "shared": shared}

    assert run(tmp_path, providers) == 0

    mirrored = [e for e in work.events.values() if e.is_mirror]
    assert [(e.title, e.description, e.location) for e in mirrored] == [("Busy", "", "")]
    assert mirrored[0].start == soon(24) - timedelta(minutes=15)
    assert mirrored[0].end == soon(25) + timedelta(minutes=15)

    assert [(e.title, e.location) for e in shared.events.values()] == [("Standup", "home")]


def test_repeated_passes_converge_and_never_ping_pong(tmp_path):
    icloud = FakeProvider([event("therapy", "Therapy", 24)])
    work = FakeProvider([event("standup", "Standup", 26)])
    shared = FakeProvider()
    providers = {"icloud": icloud, "work": work, "shared": shared}

    run(tmp_path, providers)
    counts_after_first = {name: len(p.events) for name, p in providers.items()}

    for _ in range(3):
        assert run(tmp_path, providers) == 0

    assert {name: len(p.events) for name, p in providers.items()} == counts_after_first
    assert len([e for e in shared.events.values() if e.title == "Busy"]) == 0


def test_a_cancelled_personal_event_removes_the_work_block(tmp_path):
    icloud = FakeProvider([event("therapy", "Therapy", 24)])
    providers = {"icloud": icloud, "work": FakeProvider(), "shared": FakeProvider()}
    run(tmp_path, providers)
    assert len(providers["work"].events) == 1

    icloud.events["therapy"] = dataclasses.replace(icloud.events["therapy"], cancelled=True)
    run(tmp_path, providers)
    assert providers["work"].events == {}


def test_moving_a_personal_event_moves_the_block_without_recreating_it(tmp_path):
    icloud = FakeProvider([event("therapy", "Therapy", 24)])
    providers = {"icloud": icloud, "work": FakeProvider(), "shared": FakeProvider()}
    run(tmp_path, providers)
    before = next(iter(providers["work"].events.values()))

    icloud.events["therapy"] = dataclasses.replace(icloud.events["therapy"], end=soon(26))
    run(tmp_path, providers)

    after = next(iter(providers["work"].events.values()))
    assert after.uid == before.uid
    assert after.end == soon(26) + timedelta(minutes=15)


def test_a_marker_shaped_line_in_private_notes_still_leaks_nothing(tmp_path):
    """A busy mirror is empty whatever the source description holds.

    Someone who has seen a calsync marker can paste one into their own event's
    notes. Under ``privacy: busy`` that must change nothing: the mirror's title,
    description and location are fixed regardless of the source's content, so no
    fragment of the note can reach the work calendar by any route.
    """
    secret = "oncology follow-up, Dr Reyes"
    notes = f"[calsync:0123456789abcdef:fedcba9876543210]\n{secret}"
    icloud = FakeProvider([event("therapy", "Therapy", 24, description=notes)])
    work = FakeProvider()
    providers = {"icloud": icloud, "work": work, "shared": FakeProvider()}

    assert run(tmp_path, providers) == 0

    mirrored = mirrors(work)
    # Still mirrored -- a marker-shaped line in a user's own notes must not make
    # calsync mistake their event for one of its own and silently skip it.
    assert [(e.title, e.description, e.location) for e in mirrored] == [("Busy", "", "")]
    for text in (secret, "Therapy", "private", "home"):
        assert text not in repr(work.events)


def test_the_default_filters_keep_declined_and_all_day_events_off_the_destinations(tmp_path):
    """Two defaults that would be visible mistakes, exercised through ``main``.

    A declined invitation is not something the household should see on the
    shared calendar, and an all-day personal event padded into a Busy block
    would blank out a whole working day (plus its padding) for a birthday.
    """
    holiday = CalEvent(
        uid="holiday",
        start=midnight(2),
        end=midnight(3),
        title="Bank holiday",
        all_day=True,
    )
    icloud = FakeProvider([holiday])
    work = FakeProvider(
        [
            event("standup", "Standup", 26),
            event("dentist", "Dentist", 28, declined=True),
        ]
    )
    shared = FakeProvider()
    providers = {"icloud": icloud, "work": work, "shared": shared}

    assert run(tmp_path, providers) == 0

    assert mirrors(work) == []
    assert set(work.events) == {"standup", "dentist"}
    assert [e.title for e in shared.events.values()] == ["Standup"]
    assert "Dentist" not in repr(shared.events)


def test_two_syncs_sharing_a_destination_reap_only_their_own_mirrors(tmp_path):
    """An emptied source removes that sync's mirrors and leaves the other's.

    Both syncs see the same destination calendar, and every mirror on it looks
    like a mirror. Only the sync id stamped on each one keeps a sync from
    reading its neighbour's work as destination-only and deleting it.
    """
    icloud = FakeProvider([event("therapy", "Therapy", 24)])
    work = FakeProvider([event("standup", "Standup", 26)])
    shared = FakeProvider()
    providers = {"icloud": icloud, "work": work, "shared": shared}

    assert run(tmp_path, providers, CONFIG_TWO_INTO_ONE) == 0
    by_sync = {e.marker.sync_id: e for e in mirrors(shared)}
    assert set(by_sync) == {"personal-to-shared", "work-to-shared"}
    work_mirror_uid = by_sync["work-to-shared"].uid

    icloud.events.clear()
    assert run(tmp_path, providers, CONFIG_TWO_INTO_ONE) == 0
    remaining = mirrors(shared)
    assert [e.marker.sync_id for e in remaining] == ["work-to-shared"]
    assert remaining[0].uid == work_mirror_uid

    icloud.events["therapy"] = event("therapy", "Therapy", 24)
    assert run(tmp_path, providers, CONFIG_TWO_INTO_ONE) == 0
    work.events.clear()
    assert run(tmp_path, providers, CONFIG_TWO_INTO_ONE) == 0
    assert [e.marker.sync_id for e in mirrors(shared)] == ["personal-to-shared"]
