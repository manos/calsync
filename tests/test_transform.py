from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from calsync.config import PaddingSpec, load_config
from calsync.marker import marker_line, mirror_uid, parse_key, parse_mirror_uid
from calsync.model import CalEvent, Marker
from calsync.transform import build_mirror, desired_mirrors, skip_reason

CONFIG = """
accounts:
  a: {type: google, client_id: c, client_secret: s, refresh_token: r}
  b: {type: google, client_id: c, client_secret: s, refresh_token: r}
syncs:
  - id: s1
    source: {account: a, calendar: primary}
    dest: {account: b, calendar: primary}
    padding: {before: 15m, after: 30m}
  - id: s2
    source: {account: a, calendar: primary}
    dest: {account: b, calendar: primary}
    privacy: full
"""


def spec(index: int = 0):
    return load_config(CONFIG, env={}).syncs[index]


def spec_with_skip(index: int = 0, **flags):
    """A sync spec with some of its skip filters overridden."""
    sync = spec(index)
    return sync.model_copy(update={"skip": sync.skip.model_copy(update=flags)})


def event(**kwargs) -> CalEvent:
    base = {
        "uid": "src-1",
        "start": datetime(2026, 5, 1, 9, 0, tzinfo=UTC),
        "end": datetime(2026, 5, 1, 10, 0, tzinfo=UTC),
        "title": "Dentist",
        "description": "bring insurance card",
        "location": "12 Main St",
    }
    return CalEvent(**{**base, **kwargs})


def test_busy_privacy_strips_all_detail_and_applies_padding():
    mirrors = desired_mirrors([event()], spec(0))
    (mirror,) = mirrors.values()
    assert mirror.title == "Busy"
    assert mirror.description == ""
    assert mirror.location == ""
    assert mirror.start == datetime(2026, 5, 1, 8, 45, tzinfo=UTC)
    assert mirror.end == datetime(2026, 5, 1, 10, 30, tzinfo=UTC)


def test_full_privacy_copies_detail_and_applies_no_padding_by_default():
    mirrors = desired_mirrors([event()], spec(1))
    (mirror,) = mirrors.values()
    assert mirror.title == "Dentist"
    assert mirror.description == "bring insurance card"
    assert mirror.location == "12 Main St"
    assert mirror.start == datetime(2026, 5, 1, 9, 0, tzinfo=UTC)


def test_mirror_carries_marker_keyed_on_source_and_dict_key_matches():
    mirrors = desired_mirrors([event()], spec(0))
    key, mirror = next(iter(mirrors.items()))
    assert isinstance(mirror.marker, Marker)
    assert mirror.marker.sync_id == "s1"
    assert mirror.marker.key == key
    assert mirror.marker.hash != ""


def test_padding_across_a_dst_transition_stays_wall_clock_correct():
    eastern = ZoneInfo("America/New_York")
    # 2026-03-08 01:30 EST -> 03:30 EDT; a 1h meeting spans the spring-forward gap.
    source = event(
        start=datetime(2026, 3, 8, 1, 30, tzinfo=eastern),
        end=datetime(2026, 3, 8, 3, 30, tzinfo=eastern),
    )
    (mirror,) = desired_mirrors([source], spec(0)).values()
    assert mirror.start == datetime(2026, 3, 8, 6, 15, tzinfo=UTC)
    assert mirror.end == datetime(2026, 3, 8, 8, 0, tzinfo=UTC)


def test_distinct_sources_produce_distinct_mirrors():
    mirrors = desired_mirrors([event(uid="a"), event(uid="b")], spec(0))
    assert len(mirrors) == 2


def test_skip_reasons():
    s = spec(0).skip
    assert skip_reason(event(declined=True), s) == "declined"
    assert skip_reason(event(transparent=True), s) == "free"
    assert skip_reason(event(all_day=True), s) == "all_day"
    assert skip_reason(event(cancelled=True), s) == "cancelled"
    assert skip_reason(event(marker=Marker("other", "0" * 16, "h")), s) == "mirror"
    assert skip_reason(event(), s) is None


def test_disabled_filter_lets_the_event_through():
    sync = spec_with_skip(all_day=False)
    assert skip_reason(event(all_day=True), sync.skip) is None
    assert len(desired_mirrors([event(all_day=True)], sync)) == 1


def test_mirrored_events_survive_when_the_loop_guard_is_disabled():
    # skip.mirrors is what keeps A->B and B->A from ping-ponging, but it is a flag,
    # not a law: turning it off must actually let a mirrored event through.
    mirrored = event(marker=Marker("other", "0" * 16, "h"))
    assert skip_reason(mirrored, spec_with_skip(mirrors=False).skip) is None


def test_filtered_events_never_reach_the_desired_set():
    mirrors = desired_mirrors([event(uid="a"), event(uid="b", declined=True)], spec(0))
    assert len(mirrors) == 1


# The real event that exposed this: an iCloud showing an hour and a half's drive
# away. Apple renders the drive as a block before 09:00 but leaves DTSTART at 09:00,
# so a mirror built from the span alone showed the user free the whole way there.
TRAVEL_EVENT = {
    "start": datetime(2026, 9, 18, 9, 0, tzinfo=UTC),
    "end": datetime(2026, 9, 18, 10, 0, tzinfo=UTC),
    "travel_before": timedelta(minutes=90),
}
SYMMETRIC_PADDING = PaddingSpec(before=timedelta(minutes=15), after=timedelta(minutes=15))


def spec_with_padding(padding: PaddingSpec, index: int = 0):
    return spec(index).model_copy(update={"padding": padding})


def test_travel_time_and_padding_both_extend_the_mirrors_start():
    """15m of padding on top of a 90m drive: the mirror starts 105m before the event."""
    sync = spec_with_padding(SYMMETRIC_PADDING)
    (mirror,) = desired_mirrors([event(**TRAVEL_EVENT)], sync).values()
    assert mirror.start == datetime(2026, 9, 18, 7, 15, tzinfo=UTC)
    assert mirror.end == datetime(2026, 9, 18, 10, 15, tzinfo=UTC)


def test_travel_time_alone_extends_the_start_and_leaves_the_end_alone():
    """Apple records no return journey, so nothing is added after the event."""
    sync = spec_with_padding(PaddingSpec())
    (mirror,) = desired_mirrors([event(**TRAVEL_EVENT)], sync).values()
    assert mirror.start == datetime(2026, 9, 18, 7, 30, tzinfo=UTC)
    assert mirror.end == datetime(2026, 9, 18, 10, 0, tzinfo=UTC)


def test_an_event_without_travel_time_is_padded_exactly_as_before():
    sync = spec_with_padding(SYMMETRIC_PADDING)
    without = {**TRAVEL_EVENT, "travel_before": timedelta(0)}
    (mirror,) = desired_mirrors([event(**without)], sync).values()
    assert mirror.start == datetime(2026, 9, 18, 8, 45, tzinfo=UTC)
    assert mirror.end == datetime(2026, 9, 18, 10, 15, tzinfo=UTC)


def test_travel_time_keeps_the_key_and_changes_the_hash():
    # Same reasoning as padding: the key comes from the UNTRAVELLED source start, so
    # honouring travel time updates existing mirrors instead of deleting every one
    # of them and creating a replacement.
    (with_travel,) = desired_mirrors([event(**TRAVEL_EVENT)], spec(0)).values()
    without = {**TRAVEL_EVENT, "travel_before": timedelta(0)}
    (without_travel,) = desired_mirrors([event(**without)], spec(0)).values()
    assert with_travel.start != without_travel.start
    assert with_travel.marker.key == without_travel.marker.key
    assert with_travel.marker.hash != without_travel.marker.hash


def test_travel_time_on_an_all_day_source_is_ignored():
    """Lead time on a whole-day block says nothing, and applying it would force the
    all-day form to a timed one -- which renders a day early west of Greenwich."""
    source = event(
        start=datetime(2026, 9, 18, tzinfo=UTC),
        end=datetime(2026, 9, 19, tzinfo=UTC),
        all_day=True,
        travel_before=timedelta(minutes=90),
    )
    # privacy: full, so the all-day form survives; a busy mirror is timed regardless.
    (mirror,) = desired_mirrors([source], spec_with_skip(1, all_day=False)).values()
    assert mirror.all_day is True
    assert mirror.start == datetime(2026, 9, 18, tzinfo=UTC)
    assert mirror.end == datetime(2026, 9, 19, tzinfo=UTC)


def test_changing_padding_keeps_the_key_and_changes_the_hash():
    # The key is derived from the UNPADDED source start, so widening padding has to
    # read as an update to the same mirror -- never a delete of one and a create of
    # another, which would flap the destination on every config tweak.
    padded = spec(0)
    unpadded = padded.model_copy(update={"padding": PaddingSpec()})
    (with_padding,) = desired_mirrors([event()], padded).values()
    (without_padding,) = desired_mirrors([event()], unpadded).values()
    assert with_padding.start != without_padding.start
    assert with_padding.marker.key == without_padding.marker.key
    assert with_padding.marker.hash != without_padding.marker.hash


def test_moving_only_the_end_keeps_the_key_and_changes_the_hash():
    # The key ignores the source end, so extending a meeting updates its mirror.
    (before,) = desired_mirrors([event()], spec(0)).values()
    later = event(end=datetime(2026, 5, 1, 11, 0, tzinfo=UTC))
    (after,) = desired_mirrors([later], spec(0)).values()
    assert before.marker.key == after.marker.key
    assert before.marker.hash != after.marker.hash


def test_mirror_uid_is_derived_from_the_key_not_the_source_uid():
    (mirror,) = desired_mirrors([event(uid="src-1")], spec(0)).values()
    assert mirror.uid == mirror_uid("s1", mirror.marker.key)
    assert mirror.uid != "src-1"


def test_mirror_uid_carries_the_whole_marker_identity():
    """The UID is the channel that survives a server dropping custom properties."""
    (mirror,) = desired_mirrors([event()], spec(1)).values()
    assert parse_mirror_uid(mirror.uid) == (mirror.marker.sync_id, mirror.marker.key)


def test_two_syncs_of_one_source_event_get_distinguishable_uids():
    (first,) = desired_mirrors([event()], spec(0)).values()
    (second,) = desired_mirrors([event()], spec(1)).values()
    assert parse_mirror_uid(first.uid)[0] == "s1"
    assert parse_mirror_uid(second.uid)[0] == "s2"


def test_marker_sync_id_names_the_sync_being_applied():
    # Asserted against the second sync: 's1' would also match a hardcoded id.
    (mirror,) = desired_mirrors([event()], spec(1)).values()
    assert mirror.marker.sync_id == "s2"


def test_busy_mirror_is_never_all_day_even_when_the_source_is():
    # An all-day source leaked through as an all-day mirror would advertise the
    # whole date, which is more than "busy" is allowed to say.
    source = event(
        start=datetime(2026, 5, 1, tzinfo=UTC),
        end=datetime(2026, 5, 2, tzinfo=UTC),
        all_day=True,
    )
    (mirror,) = desired_mirrors([source], spec_with_skip(all_day=False)).values()
    assert mirror.all_day is False


def test_busy_mirror_carries_no_residual_source_state():
    source = event(
        transparent=True,
        cancelled=True,
        declined=True,
        raw={"href": "/calendars/a/src-1.ics"},
    )
    mirror = build_mirror(source, spec(0))
    assert mirror.transparent is False
    assert mirror.cancelled is False
    assert mirror.declined is False
    assert mirror.raw == {}


def test_full_privacy_strips_a_marker_line_out_of_the_copied_description():
    # A source description that already looks marked must not hand its key to the
    # mirror, or the mirror would claim someone else's identity.
    source = event(description=f"bring insurance card\n\n{marker_line('0' * 16, 'f' * 16)}")
    (mirror,) = desired_mirrors([source], spec(1)).values()
    assert mirror.description == "bring insurance card"
    assert parse_key(mirror.description) is None
