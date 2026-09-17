import functools
from datetime import UTC, datetime, timedelta

import pytest
from caldav.davclient import requests as dav_http
from caldav.lib import error as dav_error

import calsync.providers.caldav as caldav_module
from calsync.config import load_config
from calsync.model import CalEvent, Marker
from calsync.providers.base import Provider, ProviderError, TransientError
from calsync.providers.caldav import CaldavProvider, _to_ical
from calsync.providers.fake import FakeProvider
from calsync.retry import retry_call
from calsync.sync import run_sync
from calsync.transform import build_mirror


@pytest.fixture(autouse=True)
def no_backoff_sleep(monkeypatch):
    """Keep the real retry policy but stop it sleeping through the test suite."""
    monkeypatch.setattr(
        caldav_module, "retry_call", functools.partial(retry_call, sleep=lambda _: None)
    )


WINDOW = (datetime(2026, 5, 1, tzinfo=UTC), datetime(2026, 5, 15, tzinfo=UTC))

TIMED = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:src-1
SUMMARY:Dentist
DESCRIPTION:bring card
LOCATION:12 Main St
DTSTART;TZID=America/New_York:20260502T090000
DTEND;TZID=America/New_York:20260502T100000
END:VEVENT
END:VCALENDAR
"""

ALL_DAY = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:src-allday
SUMMARY:Holiday
DTSTART;VALUE=DATE:20260502
DTEND;VALUE=DATE:20260503
END:VEVENT
END:VCALENDAR
"""

DURATION_ONLY = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:src-duration
SUMMARY:Call
DTSTART:20260502T130000Z
DURATION:PT45M
END:VEVENT
END:VCALENDAR
"""

BARE_TIMED = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:src-bare-timed
SUMMARY:Call
DTSTART:20260502T130000Z
END:VEVENT
END:VCALENDAR
"""

BARE_ALL_DAY = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:src-bare-allday
SUMMARY:Holiday
DTSTART;VALUE=DATE:20260502
END:VEVENT
END:VCALENDAR
"""

# Exactly as iCloud serves it: the travel time is a property of its own and
# DTSTART/DTEND are untouched, so the drive is invisible to anything reading the span.
TRAVEL = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:src-travel
SUMMARY:HTR Durango showing
DTSTART;TZID=America/Denver:20260918T090000
DTEND;TZID=America/Denver:20260918T100000
X-APPLE-TRAVEL-DURATION;VALUE=DURATION:PT1H30M
X-APPLE-TRAVEL-START;X-TITLE=Home;X-APPLE-RADIUS=100;VALUE=URI:geo:37.585568,-108.159774
END:VEVENT
END:VCALENDAR
"""

FLAGGED = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:src-flags
SUMMARY:Optional
DTSTART:20260502T130000Z
DTEND:20260502T140000Z
STATUS:CANCELLED
TRANSP:TRANSPARENT
ATTENDEE;PARTSTAT=ACCEPTED:mailto:other@example.com
ATTENDEE;PARTSTAT=DECLINED:mailto:me@example.com
END:VEVENT
END:VCALENDAR
"""

KEY = "0123456789abcdef"
HASH = "abcdef0123456789"
MIRROR_UID = f"calsync-s1-{KEY}@calsync"

MIRROR = f"""BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:{MIRROR_UID}
SUMMARY:Busy
DESCRIPTION:[calsync:{KEY}:{HASH}]
DTSTART:20260502T130000Z
DTEND:20260502T140000Z
X-CALSYNC-SYNC:s1
X-CALSYNC-KEY:{KEY}
X-CALSYNC-HASH:{HASH}
END:VEVENT
END:VCALENDAR
"""


def without_x_properties(ics: str) -> str:
    """What a server that drops unknown properties hands back (iCloud does this)."""
    return "".join(line for line in ics.splitlines(keepends=True) if "X-CALSYNC" not in line)


def without_description(ics: str) -> str:
    """What a user who cleared the description leaves behind."""
    return "".join(line for line in ics.splitlines(keepends=True) if "DESCRIPTION" not in line)


MIRROR_WITHOUT_PROPS = without_x_properties(MIRROR)
MIRROR_WITHOUT_DESCRIPTION = without_description(MIRROR)
MIRROR_WITHOUT_EITHER = without_description(MIRROR_WITHOUT_PROPS)

NEW_HASH = "f" * 16


class StubObject:
    def __init__(self, data: str, url: str):
        self.data = data
        self.url = url
        self.saved = 0
        self.deleted = False

    def save(self) -> None:
        self.saved += 1

    def delete(self) -> None:
        self.deleted = True


class StubClient:
    """The DAVClient behind a calendar; it owns the pooled HTTP session."""

    def __init__(self) -> None:
        self.closed = 0

    def close(self) -> None:
        self.closed += 1


class StubCalendar:
    def __init__(
        self,
        objects: list[StubObject] | None = None,
        error: Exception | None = None,
        fail_times: int | None = None,
        client: StubClient | None = None,
    ):
        self.objects = objects or []
        # caldav hangs the discovering DAVClient off every object it returns.
        self.client = client
        self.searches: list[dict] = []
        self.saved: list[str] = []
        self.missing_urls: set[str] = set()
        self.error = error
        # None means "fail every time"; an int makes the call flaky for that many tries.
        self.fail_times = fail_times

    def _maybe_fail(self) -> None:
        if self.error and (self.fail_times is None or len(self.searches) <= self.fail_times):
            raise self.error

    def search(self, **kwargs):
        self.searches.append(kwargs)
        self._maybe_fail()
        return self.objects

    def save_event(self, ical: str):
        self.saved.append(ical)
        return StubObject(ical, "/cal/new.ics")

    def event_by_url(self, url):
        if str(url) in self.missing_urls:
            from caldav.lib.error import NotFoundError

            raise NotFoundError(str(url))
        for obj in self.objects:
            if obj.url == str(url):
                return obj
        raise AssertionError(f"unexpected url {url}")


def provider(calendar: StubCalendar) -> CaldavProvider:
    return CaldavProvider(calendar, email="me@example.com", name="icloud")


def test_parses_a_timed_event_into_utc():
    p = provider(StubCalendar([StubObject(TIMED, "/cal/1.ics")]))
    (event,) = p.list_events(WINDOW)
    assert event.uid == "src-1"
    assert event.title == "Dentist"
    assert event.description == "bring card"
    assert event.location == "12 Main St"
    assert event.start == datetime(2026, 5, 2, 13, 0, tzinfo=UTC)
    assert event.end == datetime(2026, 5, 2, 14, 0, tzinfo=UTC)
    assert event.raw["href"] == "/cal/1.ics"


def test_parses_an_all_day_event():
    p = provider(StubCalendar([StubObject(ALL_DAY, "/cal/2.ics")]))
    (event,) = p.list_events(WINDOW)
    assert event.all_day is True
    assert event.start == datetime(2026, 5, 2, tzinfo=UTC)
    assert event.end == datetime(2026, 5, 3, tzinfo=UTC)


def test_derives_the_end_from_a_duration():
    p = provider(StubCalendar([StubObject(DURATION_ONLY, "/cal/3.ics")]))
    (event,) = p.list_events(WINDOW)
    assert event.end == datetime(2026, 5, 2, 13, 45, tzinfo=UTC)


def test_a_timed_event_with_no_end_defaults_to_one_hour():
    p = provider(StubCalendar([StubObject(BARE_TIMED, "/cal/3.ics")]))
    (event,) = p.list_events(WINDOW)
    assert event.end == datetime(2026, 5, 2, 14, 0, tzinfo=UTC)
    assert event.all_day is False


def test_an_all_day_event_with_no_end_defaults_to_one_day():
    """RFC 5545 3.6.1: a DATE-valued DTSTART alone lasts a day, not an hour."""
    p = provider(StubCalendar([StubObject(BARE_ALL_DAY, "/cal/3.ics")]))
    (event,) = p.list_events(WINDOW)
    assert event.all_day is True
    assert event.start == datetime(2026, 5, 2, tzinfo=UTC)
    assert event.end == datetime(2026, 5, 3, tzinfo=UTC)


def test_reads_apple_travel_time_as_lead_time_without_moving_the_event():
    """Apple keeps travel out of DTSTART/DTEND, so the span must come back untouched."""
    p = provider(StubCalendar([StubObject(TRAVEL, "/cal/travel.ics")]))
    (event,) = p.list_events(WINDOW)
    assert event.travel_before == timedelta(minutes=90)
    assert event.start == datetime(2026, 9, 18, 15, 0, tzinfo=UTC)
    assert event.end == datetime(2026, 9, 18, 16, 0, tzinfo=UTC)


def test_an_event_with_no_travel_property_has_no_travel_time():
    p = provider(StubCalendar([StubObject(TIMED, "/cal/1.ics")]))
    (event,) = p.list_events(WINDOW)
    assert event.travel_before == timedelta(0)


def test_a_travel_duration_stripped_of_its_value_parameter_is_still_read():
    """A server that re-serialises the property without VALUE=DURATION must not lose it."""
    ics = TRAVEL.replace("X-APPLE-TRAVEL-DURATION;VALUE=DURATION:", "X-APPLE-TRAVEL-DURATION:")
    (event,) = provider(StubCalendar([StubObject(ics, "/cal/travel.ics")])).list_events(WINDOW)
    assert event.travel_before == timedelta(minutes=90)


@pytest.mark.parametrize("value", ["PT0S", "-PT1H"], ids=["zero", "negative"])
def test_a_zero_or_negative_travel_duration_reads_as_no_travel(value: str):
    """Neither can describe a drive, and a negative one would shrink the mirror."""
    ics = TRAVEL.replace("PT1H30M", value)
    (event,) = provider(StubCalendar([StubObject(ics, "/cal/travel.ics")])).list_events(WINDOW)
    assert event.travel_before == timedelta(0)


@pytest.mark.parametrize(
    "value",
    ["banana", "20260918T150000Z"],
    ids=["not-a-duration", "a-timestamp"],
)
def test_a_malformed_travel_duration_is_reported_as_a_provider_error(value: str):
    """One unreadable property must not escape as a bare ValueError through the contract."""
    ics = TRAVEL.replace("PT1H30M", value)
    p = provider(StubCalendar([StubObject(ics, "/cal/travel.ics")]))
    with pytest.raises(ProviderError) as excinfo:
        p.list_events(WINDOW)
    message = str(excinfo.value)
    assert "/cal/travel.ics" in message
    assert "src-travel" in message
    assert "X-APPLE-TRAVEL-DURATION" in message


def test_parses_status_transparency_and_own_rsvp():
    p = provider(StubCalendar([StubObject(FLAGGED, "/cal/4.ics")]))
    (event,) = p.list_events(WINDOW)
    assert event.cancelled is True
    assert event.transparent is True
    assert event.declined is True


def test_another_attendees_decline_is_not_ours():
    ics = FLAGGED.replace(
        "PARTSTAT=DECLINED:mailto:me@example.com", "PARTSTAT=DECLINED:mailto:x@y.z"
    )
    p = provider(StubCalendar([StubObject(ics, "/cal/5.ics")]))
    (event,) = p.list_events(WINDOW)
    assert event.declined is False


def test_a_decline_is_ours_when_only_the_email_parameter_names_us():
    """iCloud and CalendarServer put a principal URI in the value, the address in EMAIL."""
    ics = FLAGGED.replace(
        "ATTENDEE;PARTSTAT=DECLINED:mailto:me@example.com",
        "ATTENDEE;PARTSTAT=DECLINED;EMAIL=me@example.com:"
        "/principals/__uids__/1F2E3D4C-5B6A-7890-ABCD-EF0123456789/",
    )
    (event,) = provider(StubCalendar([StubObject(ics, "/cal/5.ics")])).list_events(WINDOW)
    assert event.declined is True


def test_a_colleagues_decline_via_the_email_parameter_is_not_ours():
    ics = FLAGGED.replace(
        "ATTENDEE;PARTSTAT=DECLINED:mailto:me@example.com",
        "ATTENDEE;PARTSTAT=DECLINED;EMAIL=x@y.z:/principals/__uids__/AAAA/",
    )
    (event,) = provider(StubCalendar([StubObject(ics, "/cal/5.ics")])).list_events(WINDOW)
    assert event.declined is False


@pytest.mark.parametrize(
    ("configured", "on_the_invite"),
    [
        ("Me@Example.COM", "mailto:me@example.com"),
        ("me@example.com", "mailto:Me@Example.COM"),
        ("Me@Example.com", "MAILTO:ME@EXAMPLE.COM"),
    ],
)
def test_the_email_comparison_is_case_insensitive(configured: str, on_the_invite: str):
    """Servers do not agree on the case of an address; a miss would drop the skip."""
    ics = FLAGGED.replace("mailto:me@example.com", on_the_invite)
    p = CaldavProvider(StubCalendar([StubObject(ics, "/cal/5.ics")]), email=configured)
    (event,) = p.list_events(WINDOW)
    assert event.declined is True


def test_without_a_configured_email_nobodys_decline_counts_as_ours():
    """Treating any decline as the user's silently drops their own event."""
    p = CaldavProvider(StubCalendar([StubObject(FLAGGED, "/cal/5.ics")]), email="")
    (event,) = p.list_events(WINDOW)
    assert event.declined is False


def test_reads_the_marker_from_x_properties_and_strips_it_from_the_description():
    p = provider(StubCalendar([StubObject(MIRROR, "/cal/6.ics")]))
    (event,) = p.list_events(WINDOW)
    assert event.marker == Marker(sync_id="s1", key=KEY, hash=HASH)
    assert event.description == ""


def test_identity_survives_a_server_stripping_the_x_properties():
    """Sync id and key come back out of the UID, the hash out of the marker line.

    With only the key recoverable, every mirror went invisible to its own sync:
    each pass re-created it and none was ever reaped.
    """
    p = provider(StubCalendar([StubObject(MIRROR_WITHOUT_PROPS, "/cal/7.ics")]))
    (event,) = p.list_events(WINDOW)
    assert event.marker == Marker(sync_id="s1", key=KEY, hash=HASH)


def test_identity_survives_a_cleared_description():
    p = provider(StubCalendar([StubObject(MIRROR_WITHOUT_DESCRIPTION, "/cal/7.ics")]))
    (event,) = p.list_events(WINDOW)
    assert event.marker == Marker(sync_id="s1", key=KEY, hash=HASH)


def test_the_uid_alone_still_matches_a_mirror_to_its_sync():
    """Both channels gone: the mirror is updated rather than duplicated."""
    p = provider(StubCalendar([StubObject(MIRROR_WITHOUT_EITHER, "/cal/7.ics")]))
    (event,) = p.list_events(WINDOW)
    assert event.marker == Marker(sync_id="s1", key=KEY, hash="")


@pytest.mark.parametrize(
    "ics",
    [MIRROR, MIRROR_WITHOUT_PROPS, MIRROR_WITHOUT_DESCRIPTION, MIRROR_WITHOUT_EITHER],
    ids=["intact", "no-x-props", "no-description", "neither"],
)
def test_list_mirrors_finds_the_mirror_however_much_the_server_stripped(ics: str):
    mirrors = provider(StubCalendar([StubObject(ics, "/cal/7.ics")])).list_mirrors(WINDOW, "s1")
    assert [m.marker.key for m in mirrors] == [KEY]


def test_a_uid_from_another_sync_is_not_claimed_by_this_one():
    ics = without_x_properties(MIRROR).replace("calsync-s1-", "calsync-s2-")
    p = provider(StubCalendar([StubObject(ics, "/cal/7.ics")]))
    (event,) = p.list_events(WINDOW)
    assert event.marker.sync_id == "s2"
    assert p.list_mirrors(WINDOW, "s1") == []


def test_a_foreign_uid_is_not_read_as_calsync_identity():
    """A user's own event must never be mistaken for a mirror, or it is never synced."""
    ics = without_description(without_x_properties(MIRROR)).replace(MIRROR_UID, "some-other-uid")
    (event,) = provider(StubCalendar([StubObject(ics, "/cal/7.ics")])).list_events(WINDOW)
    assert event.marker is None


NO_DTSTART = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:src-no-start
SUMMARY:Mystery
END:VEVENT
END:VCALENDAR
"""

END_BEFORE_START = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:src-backwards
SUMMARY:Backwards
DTSTART:20260502T140000Z
DTEND:20260502T130000Z
END:VEVENT
END:VCALENDAR
"""

UNDECODABLE_DTSTART = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:src-garbage
SUMMARY:Garbage
DTSTART:not-a-timestamp
END:VEVENT
END:VCALENDAR
"""


@pytest.mark.parametrize(
    "ics",
    [NO_DTSTART, END_BEFORE_START, UNDECODABLE_DTSTART, "this is not iCalendar at all"],
    ids=["no-dtstart", "end-before-start", "undecodable-dtstart", "not-ical"],
)
def test_a_malformed_object_is_reported_as_a_provider_error_naming_the_href(ics: str):
    """One bad VEVENT must not take the pass down with a bare KeyError/ValueError."""
    p = provider(StubCalendar([StubObject(ics, "/cal/broken.ics")]))
    with pytest.raises(ProviderError) as excinfo:
        p.list_events(WINDOW)
    assert "/cal/broken.ics" in str(excinfo.value)


def test_search_expands_recurrences_over_the_window():
    calendar = StubCalendar()
    provider(calendar).list_events(WINDOW)
    (search,) = calendar.searches
    assert search["expand"] is True
    assert search["event"] is True
    assert search["start"] == WINDOW[0]
    assert search["end"] == WINDOW[1]


def test_list_mirrors_returns_only_this_syncs_mirrors():
    other = MIRROR.replace("X-CALSYNC-SYNC:s1", "X-CALSYNC-SYNC:s2").replace(
        f"calsync-s1-{KEY}", "calsync-s2-fedcba9876543210"
    )
    calendar = StubCalendar(
        [
            StubObject(TIMED, "/cal/1.ics"),
            StubObject(MIRROR, "/cal/6.ics"),
            StubObject(other, "/cal/8.ics"),
        ]
    )
    mirrors = provider(calendar).list_mirrors(WINDOW, "s1")
    assert [m.marker.key for m in mirrors] == [KEY]


def test_create_writes_a_stamped_vevent_with_a_deterministic_uid():
    calendar = StubCalendar()
    mirror = CalEvent(
        uid=MIRROR_UID,
        start=datetime(2026, 5, 2, 13, 0, tzinfo=UTC),
        end=datetime(2026, 5, 2, 14, 0, tzinfo=UTC),
        title="Busy",
        marker=Marker("s1", KEY, HASH),
    )
    provider(calendar).create(mirror)

    (ical,) = calendar.saved
    assert f"UID:{MIRROR_UID}" in ical
    assert "SUMMARY:Busy" in ical
    assert "X-CALSYNC-SYNC:s1" in ical
    assert f"X-CALSYNC-KEY:{KEY}" in ical
    assert f"X-CALSYNC-HASH:{HASH}" in ical
    assert f"[calsync:{KEY}:{HASH}]" in ical
    # DATE-TIME is the RFC 5545 default value type for DTSTART, so icalendar writes
    # no redundant VALUE parameter; the trailing Z is what makes it UTC.
    assert "DTSTART:20260502T130000Z" in ical
    # A transparent mirror would reserve no time at all, which is the whole point.
    assert "TRANSP:OPAQUE" in ical


def test_update_saves_over_the_existing_href():
    existing = StubObject(MIRROR, "/cal/6.ics")
    calendar = StubCalendar([existing])
    mirror = CalEvent(
        uid=MIRROR_UID,
        start=datetime(2026, 5, 2, 13, 0, tzinfo=UTC),
        end=datetime(2026, 5, 2, 15, 0, tzinfo=UTC),
        title="Busy",
        marker=Marker("s1", KEY, NEW_HASH),
        raw={"href": "/cal/6.ics"},
    )
    provider(calendar).update(mirror)

    assert existing.saved == 1
    assert f"X-CALSYNC-HASH:{NEW_HASH}" in existing.data
    assert "DTEND:20260502T150000Z" in existing.data


def test_update_writes_the_new_data_before_saving_it():
    """save() after the assignment, or the round trip persists the stale body."""

    class SnapshottingObject(StubObject):
        def __init__(self, data: str, url: str):
            super().__init__(data, url)
            self.data_at_save: list[str] = []

        def save(self) -> None:
            self.data_at_save.append(self.data)
            super().save()

    existing = SnapshottingObject(MIRROR, "/cal/6.ics")
    provider(StubCalendar([existing])).update(
        mirror(marker=Marker("s1", KEY, NEW_HASH), raw={"href": "/cal/6.ics"})
    )

    (at_save,) = existing.data_at_save
    assert f"X-CALSYNC-HASH:{NEW_HASH}" in at_save


def test_update_without_an_href_is_an_error():
    mirror = CalEvent(
        uid=MIRROR_UID,
        start=datetime(2026, 5, 2, 13, 0, tzinfo=UTC),
        end=datetime(2026, 5, 2, 14, 0, tzinfo=UTC),
        marker=Marker("s1", KEY, NEW_HASH),
    )
    with pytest.raises(ProviderError, match="href"):
        provider(StubCalendar()).update(mirror)


def test_delete_removes_the_object_and_tolerates_a_missing_one():
    existing = StubObject(MIRROR, "/cal/6.ics")
    calendar = StubCalendar([existing])
    mirror = CalEvent(
        uid=MIRROR_UID,
        start=datetime(2026, 5, 2, 13, 0, tzinfo=UTC),
        end=datetime(2026, 5, 2, 14, 0, tzinfo=UTC),
        marker=Marker("s1", KEY, NEW_HASH),
        raw={"href": "/cal/6.ics"},
    )
    provider(calendar).delete(mirror)
    assert existing.deleted is True

    calendar.missing_urls.add("/cal/6.ics")
    provider(calendar).delete(mirror)  # must not raise


def test_delete_without_an_href_is_an_error():
    """Silently skipping it would leave the mirror standing and report a deletion."""
    unaddressed = CalEvent(
        uid=MIRROR_UID,
        start=datetime(2026, 5, 2, 13, 0, tzinfo=UTC),
        end=datetime(2026, 5, 2, 14, 0, tzinfo=UTC),
        marker=Marker("s1", KEY, HASH),
    )
    with pytest.raises(ProviderError, match="href"):
        provider(StubCalendar()).delete(unaddressed)


def mirror(**overrides) -> CalEvent:
    base = {
        "uid": MIRROR_UID,
        "start": datetime(2026, 5, 2, 13, 0, tzinfo=UTC),
        "end": datetime(2026, 5, 2, 14, 0, tzinfo=UTC),
        "title": "Busy",
        "marker": Marker("s1", KEY, HASH),
    }
    return CalEvent(**{**base, **overrides})


@pytest.mark.parametrize(
    ("title", "description", "location"),
    [("Busy", "", ""), ("Dentist", "bring card", "12 Main St")],
    ids=["busy-mirror", "detail-mirror"],
)
def test_a_written_mirror_reads_back_with_the_same_marker(
    title: str, description: str, location: str
):
    """The marker must survive the write/read round trip, or the loop guard fails."""
    source = mirror(title=title, description=description, location=location)
    stored = _to_ical(source)

    (read_back,) = provider(StubCalendar([StubObject(stored, "/cal/rt.ics")])).list_events(WINDOW)

    assert read_back.marker == source.marker
    assert read_back.title == source.title
    assert read_back.description == source.description
    assert read_back.location == source.location
    assert (read_back.start, read_back.end) == (source.start, source.end)


def test_an_all_day_mirror_is_written_as_a_date():
    """A timed block at UTC midnight renders a day early west of Greenwich."""
    ical = _to_ical(
        mirror(
            start=datetime(2026, 5, 2, tzinfo=UTC),
            end=datetime(2026, 5, 4, tzinfo=UTC),
            all_day=True,
        )
    )
    assert "DTSTART;VALUE=DATE:20260502" in ical
    # DTEND is exclusive in the DATE form, which is how _as_utc reads it back.
    assert "DTEND;VALUE=DATE:20260504" in ical


def test_a_written_all_day_mirror_reads_back_as_the_same_all_day_event():
    source = mirror(
        start=datetime(2026, 5, 2, tzinfo=UTC), end=datetime(2026, 5, 4, tzinfo=UTC), all_day=True
    )
    stored = _to_ical(source)
    (read_back,) = provider(StubCalendar([StubObject(stored, "/cal/rt.ics")])).list_events(WINDOW)
    assert read_back.all_day is True
    assert (read_back.start, read_back.end) == (source.start, source.end)


def test_a_padded_all_day_mirror_keeps_the_timed_form():
    """Padding moved the edges off midnight; the DATE form would silently drop it."""
    ical = _to_ical(
        mirror(
            start=datetime(2026, 5, 1, 23, 30, tzinfo=UTC),
            end=datetime(2026, 5, 4, 0, 30, tzinfo=UTC),
            all_day=True,
        )
    )
    assert "DTSTART:20260501T233000Z" in ical
    assert "DTEND:20260504T003000Z" in ical


@pytest.mark.parametrize("write", ["create", "update"])
def test_writing_an_unmarked_event_is_refused(write: str):
    """calsync never leaves an untracked event on a real calendar."""
    calendar = StubCalendar()
    unmarked = CalEvent(
        uid=MIRROR_UID,
        start=datetime(2026, 5, 2, 13, 0, tzinfo=UTC),
        end=datetime(2026, 5, 2, 14, 0, tzinfo=UTC),
        title="Busy",
        raw={"href": "/cal/6.ics"},
    )
    # The refusal has to come before any round trip. StubCalendar holds no objects, so
    # an update that reached event_by_url would fail with "unexpected url" instead.
    with pytest.raises(ProviderError, match="marker"):
        getattr(provider(calendar), write)(unmarked)
    assert calendar.saved == []


def test_the_x_property_wins_over_a_disagreeing_description_marker():
    """The property is what calsync wrote last; prose can be edited by anyone."""
    ics = MIRROR.replace(
        f"DESCRIPTION:[calsync:{KEY}:{HASH}]",
        "DESCRIPTION:[calsync:ffffffffffffffff:0000000000000000]",
    )
    p = provider(StubCalendar([StubObject(ics, "/cal/9.ics")]))
    (event,) = p.list_events(WINDOW)
    assert event.marker.key == KEY
    assert event.marker.hash == HASH


def test_update_against_a_vanished_href_raises_a_provider_error():
    """Unlike a delete, a missing target means the write cannot be expressed at all."""
    calendar = StubCalendar()
    calendar.missing_urls.add("/cal/gone.ics")
    with pytest.raises(ProviderError) as excinfo:
        provider(calendar).update(mirror(raw={"href": "/cal/gone.ics"}))
    # Callers discriminate on the status, not on message text, which carries hrefs.
    assert excinfo.value.status == 404


def dav_error_for(status: int, reason: str, body: str = "") -> dav_error.DAVError:
    """A caldav error shaped the way caldav builds them: errmsg() in the url slot."""
    return dav_error.ReportError(f"{status} {reason}\n\n{body}")


def test_server_errors_become_transient_and_client_errors_do_not():
    with pytest.raises(TransientError) as transient:
        provider(StubCalendar(error=dav_error_for(503, "Service Unavailable"))).list_events(WINDOW)
    assert transient.value.status == 503
    with pytest.raises(ProviderError) as excinfo:
        provider(StubCalendar(error=dav_error_for(400, "Bad Request"))).list_events(WINDOW)
    assert not isinstance(excinfo.value, TransientError)
    assert excinfo.value.status == 400


def test_a_rate_limit_error_is_transient():
    """caldav raises RateLimitError for 429, carrying the URL rather than a status."""
    error = dav_error.RateLimitError(
        url="https://caldav.icloud.com/1/calendars/x", reason="slow down"
    )
    with pytest.raises(TransientError):
        provider(StubCalendar(error=error)).list_events(WINDOW)


def test_a_denial_is_permanent_and_is_not_retried():
    """Retrying a calendar we are not allowed to read just burns the pass, forever.

    This is the shape caldav really raises for a 401/403: an AuthorizationError
    carrying the request URL, not errmsg output -- so there is no leading status
    token to read, and the "503" in the URL must not make it look transient.
    """
    calendar = StubCalendar(
        error=dav_error.AuthorizationError(
            url="https://caldav.icloud.com/1/calendars/503/", reason="Forbidden"
        )
    )
    with pytest.raises(ProviderError) as excinfo:
        provider(calendar).list_events(WINDOW)
    assert not isinstance(excinfo.value, TransientError)
    assert excinfo.value.status == 403
    assert len(calendar.searches) == 1


def test_a_transient_failure_is_retried_until_it_succeeds():
    calendar = StubCalendar(
        [StubObject(TIMED, "/cal/1.ics")],
        error=dav_error_for(503, "Service Unavailable"),
        fail_times=2,
    )
    (event,) = provider(calendar).list_events(WINDOW)
    assert event.uid == "src-1"
    assert len(calendar.searches) == 3


def test_a_connection_timeout_is_transient_and_is_retried():
    """caldav's HTTP layer is niquests, whose exceptions are not requests'.

    Catching requests' exceptions caught nothing at all: an iCloud connection
    timeout escaped unwrapped and unretried through the Provider contract.
    """
    calendar = StubCalendar(
        [StubObject(TIMED, "/cal/1.ics")],
        error=dav_http.ConnectionError("connection to caldav.icloud.com timed out"),
        fail_times=2,
    )
    (event,) = provider(calendar).list_events(WINDOW)
    assert event.uid == "src-1"
    assert len(calendar.searches) == 3


def test_a_read_timeout_that_never_clears_surfaces_as_a_transient_error():
    with pytest.raises(TransientError):
        provider(StubCalendar(error=dav_http.Timeout("read timed out"))).list_events(WINDOW)


def test_caldav_provider_satisfies_the_protocol():
    assert isinstance(provider(StubCalendar()), Provider)


# -- convergence over repeated passes ----------------------------------------------

SYNC_CONFIG = """
accounts:
  a: {type: google, client_id: c, client_secret: s, refresh_token: r}
  b: {type: caldav, url: https://caldav.icloud.com, username: u, password: p}
syncs:
  - id: s1
    source: {account: a, calendar: primary}
    dest: {account: b, calendar: Mirror}
"""
NOW = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)


class LossyObject(StubObject):
    """An object whose server drops the same channels on every write, create or not."""

    def __init__(self, data: str, url: str, drop):
        self._drop = drop
        super().__init__(drop(data), url)

    def save(self) -> None:
        self.data = self._drop(self.data)
        super().save()


class LossyCalendar(StubCalendar):
    """A server that persists what it is given, minus the channels it drops.

    iCloud really does drop unknown properties on some calendars, and a user can
    always clear a description by hand.
    """

    def __init__(self, drop_properties: bool = False, drop_description: bool = False):
        super().__init__()
        self._drop_properties = drop_properties
        self._drop_description = drop_description
        self._stored = 0

    def _drop(self, ical: str) -> str:
        if self._drop_properties:
            ical = without_x_properties(ical)
        if self._drop_description:
            ical = without_description(ical)
        return ical

    def save_event(self, ical: str):
        self.saved.append(ical)
        self._stored += 1
        obj = LossyObject(ical, f"/cal/stored-{self._stored}.ics", self._drop)
        self.objects.append(obj)
        return obj


def run_passes(
    calendar: LossyCalendar, count: int, travel: timedelta = timedelta(0)
) -> list[tuple[int, int, int]]:
    spec = load_config(SYNC_CONFIG, env={}).syncs[0]
    source = FakeProvider(
        [
            CalEvent(
                uid="src-1",
                start=datetime(2026, 5, 2, 9, 0, tzinfo=UTC),
                end=datetime(2026, 5, 2, 10, 0, tzinfo=UTC),
                title="Dentist",
                travel_before=travel,
            )
        ]
    )
    dest = CaldavProvider(calendar, email="me@example.com")
    results = [run_sync(spec, source, dest, now=NOW) for _ in range(count)]
    return [(r.created, r.updated, r.deleted) for r in results]


@pytest.mark.parametrize(
    ("drop_properties", "drop_description"),
    [(False, False), (True, False), (False, True)],
    ids=["nothing-dropped", "x-properties-dropped", "description-dropped"],
)
def test_repeated_passes_converge_on_one_mirror(drop_properties: bool, drop_description: bool):
    """The mirror is created once and then left alone, whichever channel survives.

    When identity lived only in the X- properties plus a key-only description line,
    dropping the properties made every mirror invisible to its own sync: each pass
    re-created it, none was ever reaped, and the delete rail never tripped because
    there were zero deletions. Five passes left five events on the calendar.
    """
    calendar = LossyCalendar(drop_properties, drop_description)
    assert run_passes(calendar, 5) == [(1, 0, 0)] + [(0, 0, 0)] * 4
    assert len(calendar.objects) == 1


def test_a_mirror_that_lost_both_channels_is_updated_not_duplicated():
    """With the hash gone the content is unknown, so each pass rewrites it in place.

    An update every pass is wasteful; a duplicate every pass is unusable.
    """
    calendar = LossyCalendar(drop_properties=True, drop_description=True)
    assert run_passes(calendar, 5) == [(1, 0, 0)] + [(0, 1, 0)] * 4
    assert len(calendar.objects) == 1


def test_a_mirror_of_a_travelling_event_carries_no_travel_time_of_its_own():
    """The drive is baked into the mirror's start, so the mirror must not restate it.

    A mirror that advertised the travel duration as a property of its own would hand
    the next sync in an A->B->C chain a lead time it has already been paid: B's
    mirror would start 90 minutes before A's mirror, which already started 90
    minutes before the event.
    """
    spec = load_config(SYNC_CONFIG, env={}).syncs[0]
    source = CalEvent(
        uid="src-travel",
        start=datetime(2026, 5, 2, 9, 0, tzinfo=UTC),
        end=datetime(2026, 5, 2, 10, 0, tzinfo=UTC),
        title="HTR Durango showing",
        travel_before=timedelta(minutes=90),
    )
    written = build_mirror(source, spec)
    assert written.travel_before == timedelta(0)
    assert written.start == datetime(2026, 5, 2, 7, 30, tzinfo=UTC)

    stored = _to_ical(written)
    assert "X-APPLE-TRAVEL-DURATION" not in stored

    (read_back,) = provider(StubCalendar([StubObject(stored, "/cal/rt.ics")])).list_events(WINDOW)
    assert read_back.travel_before == timedelta(0)
    assert read_back.start == written.start


def test_repeated_passes_over_a_travelling_event_do_not_accumulate_travel_time():
    """Five passes through a real iCalendar round trip leave the start where it was.

    The mirror is written, read back, and compared against a freshly built one on
    every pass. If any of that re-derived the drive, the start would walk 90 minutes
    earlier each time and each pass would report an update.
    """
    calendar = LossyCalendar()
    assert run_passes(calendar, 5, travel=timedelta(minutes=90)) == [(1, 0, 0)] + [(0, 0, 0)] * 4
    (stored,) = calendar.objects
    assert "DTSTART:20260502T073000Z" in stored.data
    assert "X-APPLE-TRAVEL-DURATION" not in stored.data


def test_closing_the_provider_closes_the_http_session():
    # run_pass builds providers once per pass, so a session nobody closes is a
    # leak that grows with uptime: one per CalDAV calendar per pass.
    client = StubClient()
    CaldavProvider(StubCalendar(client=client), email="me@example.com").close()
    assert client.closed == 1


def test_closing_a_provider_whose_calendar_has_no_client_is_a_no_op():
    # Nothing to release, and a close() that raised here would be noise in the
    # runner's log on every pass.
    provider(StubCalendar()).close()
