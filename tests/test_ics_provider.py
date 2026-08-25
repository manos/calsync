import functools
import gzip
import time
import urllib.error
from datetime import UTC, datetime

import pytest

import calsync.providers.ics as ics_module
from calsync.model import CalEvent, Marker
from calsync.providers.base import Provider, ProviderError, TransientError
from calsync.providers.ics import IcsProvider, fetch_url, scrub
from calsync.retry import retry_call


@pytest.fixture(autouse=True)
def no_backoff_sleep(monkeypatch):
    """Keep the real retry policy but stop it sleeping through the test suite."""
    monkeypatch.setattr(
        ics_module, "retry_call", functools.partial(retry_call, sleep=lambda _: None)
    )


WINDOW = (datetime(2026, 5, 1, tzinfo=UTC), datetime(2026, 5, 15, tzinfo=UTC))
# Everything after the host is the credential: this is the shape of a TripIt
# private feed, whose path is enough for anyone to read the user's travel.
TOKEN = "not-a-real-token"
URL = f"https://www.tripit.com/feed/ical/private/{TOKEN}/tripit.ics"

EMPTY = """BEGIN:VCALENDAR
VERSION:2.0
END:VCALENDAR
"""

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

FLOATING = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:src-floating
SUMMARY:Somewhere
DTSTART:20260502T090000
DTEND:20260502T100000
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

BARE_ALL_DAY = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:src-bare-allday
SUMMARY:Holiday
DTSTART;VALUE=DATE:20260502
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
END:VEVENT
END:VCALENDAR
"""

ONE_HOUR = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:src-window
SUMMARY:Meeting
DTSTART:20260502T100000Z
DTEND:20260502T110000Z
END:VEVENT
END:VCALENDAR
"""

OUT_OF_ORDER = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:later
SUMMARY:Later
DTSTART:20260510T090000Z
DTEND:20260510T100000Z
END:VEVENT
BEGIN:VEVENT
UID:earlier
SUMMARY:Earlier
DTSTART:20260502T090000Z
DTEND:20260502T100000Z
END:VEVENT
END:VCALENDAR
"""

# A weekly standup, one occurrence cancelled outright and one moved to a later
# hour under a different title -- the three ways a feed states a series.
RECURRING = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:weekly-1
SUMMARY:Standup
DTSTART;TZID=America/New_York:20260504T090000
DTEND;TZID=America/New_York:20260504T091500
RRULE:FREQ=WEEKLY;COUNT=4
EXDATE;TZID=America/New_York:20260511T090000
END:VEVENT
BEGIN:VEVENT
UID:weekly-1
RECURRENCE-ID;TZID=America/New_York:20260518T090000
SUMMARY:Standup (moved)
DTSTART;TZID=America/New_York:20260518T113000
DTEND;TZID=America/New_York:20260518T120000
END:VEVENT
END:VCALENDAR
"""
RECURRING_WINDOW = (datetime(2026, 5, 1, tzinfo=UTC), datetime(2026, 6, 1, tzinfo=UTC))

KEY = "0123456789abcdef"
HASH = "abcdef0123456789"
MIRROR = f"""BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:calsync-s1-{KEY}@calsync
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

OTHER_KEY = "fedcba9876543210"
# The two identity channels disagreeing: the UID says one key, the description's
# marker line says another. Only the X-CALSYNC-* properties outrank the UID, and
# this VEVENT carries none.
UID_AND_LINE_DISAGREE = f"""BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:calsync-s1-{KEY}@calsync
SUMMARY:Busy
DESCRIPTION:[calsync:{OTHER_KEY}:{HASH}]
DTSTART:20260502T130000Z
DTEND:20260502T140000Z
END:VEVENT
END:VCALENDAR
"""

# One tick per second, forever: no COUNT, no UNTIL. Over a fortnight that is more
# than a million occurrences, and every one of them would be created on the user's
# real calendar.
RUNAWAY = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:runaway
SUMMARY:Tick
DTSTART:20260502T000000Z
DTEND:20260502T000100Z
RRULE:FREQ=SECONDLY
END:VEVENT
END:VCALENDAR
"""


class StubFeed:
    """The HTTP fetch, without the HTTP: it records its calls and can misbehave."""

    def __init__(
        self,
        ics: str = EMPTY,
        error: Exception | None = None,
        fail_times: int | None = None,
    ):
        self.ics = ics
        self.urls: list[str] = []
        self.error = error
        # None means "fail every time"; an int makes the feed flaky for that many tries.
        self.fail_times = fail_times

    def __call__(self, url: str) -> bytes:
        self.urls.append(url)
        if self.error and (self.fail_times is None or len(self.urls) <= self.fail_times):
            raise self.error
        return self.ics.encode()


def provider(feed: StubFeed, url: str = URL) -> IcsProvider:
    return IcsProvider(url, name="trips", fetch=feed)


def test_parses_a_timed_event_into_utc():
    (event,) = provider(StubFeed(TIMED)).list_events(WINDOW)
    assert event.uid == "src-1"
    assert event.title == "Dentist"
    assert event.description == "bring card"
    assert event.location == "12 Main St"
    assert event.start == datetime(2026, 5, 2, 13, 0, tzinfo=UTC)
    assert event.end == datetime(2026, 5, 2, 14, 0, tzinfo=UTC)
    assert event.all_day is False


def test_a_floating_time_is_read_as_utc():
    """RFC 5545's "local time wherever it is viewed" has no answer here.

    calsync keeps no state and does not know the feed's zone; the host's own zone
    would rekey every occurrence whenever the host moved. UTC is wrong by a fixed
    offset, local is wrong unpredictably.
    """
    (event,) = provider(StubFeed(FLOATING)).list_events(WINDOW)
    assert event.start == datetime(2026, 5, 2, 9, 0, tzinfo=UTC)


def test_parses_an_all_day_event():
    (event,) = provider(StubFeed(ALL_DAY)).list_events(WINDOW)
    assert event.all_day is True
    assert event.start == datetime(2026, 5, 2, tzinfo=UTC)
    assert event.end == datetime(2026, 5, 3, tzinfo=UTC)


def test_an_all_day_event_with_no_end_spans_one_day():
    """RFC 5545 3.6.1: a DATE-valued DTSTART alone lasts a day, not an hour."""
    (event,) = provider(StubFeed(BARE_ALL_DAY)).list_events(WINDOW)
    assert event.all_day is True
    assert event.start == datetime(2026, 5, 2, tzinfo=UTC)
    assert event.end == datetime(2026, 5, 3, tzinfo=UTC)


def test_an_all_day_event_that_ends_where_it_starts_spans_one_day():
    """RFC 5545 3.6.1 again: DTEND is exclusive, so DATE:X..DATE:X names that day.

    Feeds emit it, and read literally it would be a zero-length all-day event --
    which reserves nothing and would mirror as nothing at all.
    """
    ics = ALL_DAY.replace("DTEND;VALUE=DATE:20260503", "DTEND;VALUE=DATE:20260502")
    (event,) = provider(StubFeed(ics)).list_events(WINDOW)
    assert event.all_day is True
    assert event.start == datetime(2026, 5, 2, tzinfo=UTC)
    assert event.end == datetime(2026, 5, 3, tzinfo=UTC)


def test_parses_status_and_transparency():
    (event,) = provider(StubFeed(FLAGGED)).list_events(WINDOW)
    assert event.cancelled is True
    assert event.transparent is True


def test_status_and_transparency_are_read_case_insensitively():
    """RFC 5545 property values are case-insensitive, and feeds do write them mixed."""
    ics = FLAGGED.replace("STATUS:CANCELLED", "STATUS:Cancelled")
    ics = ics.replace("TRANSP:TRANSPARENT", "TRANSP:Transparent")
    (event,) = provider(StubFeed(ics)).list_events(WINDOW)
    assert event.cancelled is True
    assert event.transparent is True


def test_nothing_in_a_feed_counts_as_declined_by_us():
    """A feed carries no identity for the reader, so no RSVP on it can be theirs."""
    ics = FLAGGED.replace("TRANSP:TRANSPARENT", "ATTENDEE;PARTSTAT=DECLINED:mailto:me@example.com")
    (event,) = provider(StubFeed(ics)).list_events(WINDOW)
    assert event.declined is False


def test_expands_a_recurring_event_honouring_exdate_and_an_override():
    events = provider(StubFeed(RECURRING)).list_events(RECURRING_WINDOW)
    assert [(event.start, event.title) for event in events] == [
        (datetime(2026, 5, 4, 13, 0, tzinfo=UTC), "Standup"),
        # 2026-05-11 is EXDATEd out; 2026-05-18 moved to 11:30 under a new title.
        (datetime(2026, 5, 18, 15, 30, tzinfo=UTC), "Standup (moved)"),
        (datetime(2026, 5, 25, 13, 0, tzinfo=UTC), "Standup"),
    ]


def test_a_runaway_recurrence_is_refused_rather_than_expanded():
    """A feed is the first source calsync does not control, and it sets the work.

    ``FREQ=SECONDLY`` with no COUNT and no UNTIL is over a million occurrences in
    a fortnight, and every one of them would be a create against the user's real
    calendar -- which the delete rail does not cover. The cap is far above any
    real calendar and far below harm.
    """
    events = None
    started = time.monotonic()
    with pytest.raises(ProviderError) as excinfo:
        events = provider(StubFeed(RUNAWAY)).list_events(WINDOW)
    elapsed = time.monotonic() - started

    message = str(excinfo.value)
    assert str(ics_module.MAX_OCCURRENCES) in message
    # Names which feed, by host, like every other failure here.
    assert "www.tripit.com" in message
    assert TOKEN not in message
    # Retrying cannot make a runaway RRULE smaller.
    assert not isinstance(excinfo.value, TransientError)
    # Nothing comes back, not even the occurrences up to the cap: a truncated read
    # would make every occurrence past it look destination-only and get it deleted.
    assert events is None
    # The cap has to fire while the occurrences are still being produced. Expanding
    # this feed across the whole window took 313 seconds and 14 GB.
    assert elapsed < 5.0


def test_a_feed_right_at_the_cap_is_still_read(monkeypatch):
    """The cap is a ceiling, not a target: exactly ``MAX_OCCURRENCES`` is fine."""
    monkeypatch.setattr(ics_module, "MAX_OCCURRENCES", 3)
    ics = RECURRING.replace("RRULE:FREQ=WEEKLY;COUNT=4", "RRULE:FREQ=WEEKLY;COUNT=3")
    ics = ics.replace("EXDATE;TZID=America/New_York:20260511T090000\n", "")
    assert len(provider(StubFeed(ics)).list_events(RECURRING_WINDOW)) == 3


def test_every_occurrence_of_a_series_keeps_the_series_uid():
    """The differ keys on (sync id, uid, occurrence start), so this is not a clash."""
    events = provider(StubFeed(RECURRING)).list_events(RECURRING_WINDOW)
    assert {event.uid for event in events} == {"weekly-1"}


@pytest.mark.parametrize(
    ("window", "expected"),
    [
        ((datetime(2026, 5, 2, 9, tzinfo=UTC), datetime(2026, 5, 2, 10, tzinfo=UTC)), 0),
        ((datetime(2026, 5, 2, 11, tzinfo=UTC), datetime(2026, 5, 2, 12, tzinfo=UTC)), 0),
        ((datetime(2026, 5, 2, 10, 59, tzinfo=UTC), datetime(2026, 5, 2, 12, tzinfo=UTC)), 1),
        ((datetime(2026, 5, 2, 9, tzinfo=UTC), datetime(2026, 5, 2, 10, 1, tzinfo=UTC)), 1),
    ],
    ids=["ends-at-window-start", "starts-at-window-end", "overlaps-start", "overlaps-end"],
)
def test_the_window_is_half_open(window, expected):
    """Same rule as the other providers, or an edge occurrence rekeys per backend."""
    assert len(provider(StubFeed(ONE_HOUR)).list_events(window)) == expected


SPANS_THE_WINDOW = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:conference
SUMMARY:Long haul
DTSTART:20260420T090000Z
DTEND:20260512T170000Z
END:VEVENT
BEGIN:VEVENT
UID:later
SUMMARY:Later
DTSTART:20260513T090000Z
DTEND:20260513T100000Z
END:VEVENT
END:VCALENDAR
"""


def test_an_event_that_started_before_the_window_is_still_found():
    """Expansion walks forward from the window's start, so this is the case it could miss.

    A three-week conference starting eleven days before the window overlaps it
    without starting in it, and the walk must not skip it -- nor stop at it and
    lose everything after.
    """
    events = provider(StubFeed(SPANS_THE_WINDOW)).list_events(WINDOW)
    assert [event.title for event in events] == ["Long haul", "Later"]
    assert events[0].start == datetime(2026, 4, 20, 9, 0, tzinfo=UTC)


def test_events_come_back_in_start_order():
    """The Provider contract promises it; a feed is in whatever order it was written."""
    events = provider(StubFeed(OUT_OF_ORDER)).list_events(WINDOW)
    assert [event.title for event in events] == ["Earlier", "Later"]


def test_a_calsync_mirror_in_a_feed_is_recognised_as_one():
    """A published calendar can itself be a calsync destination.

    Unrecognised, the mirror would be mirrored again on every pass: skip.mirrors
    is the guard that keeps that loop shut, and it reads the marker.
    """
    (event,) = provider(StubFeed(MIRROR)).list_events(WINDOW)
    assert event.marker == Marker(sync_id="s1", key=KEY, hash=HASH)
    assert event.description == ""


def test_the_uid_outranks_the_description_marker_line_for_the_key():
    """A UID is the object's identity; a description is prose a user can edit.

    Read the other way round, an edited marker line would rekey the mirror and
    the sync would delete the real one and write a duplicate.
    """
    (event,) = provider(StubFeed(UID_AND_LINE_DISAGREE)).list_events(WINDOW)
    assert event.marker == Marker(sync_id="s1", key=KEY, hash=HASH)


def test_a_zero_length_timed_event_stays_zero_length():
    """calsync does not invent busy time that the feed did not state.

    recurring_ical_events resolves "no DTEND, no DURATION" to an occurrence
    ending exactly where it starts. Padding that to an hour would write an hour
    of busy time onto the user's calendar that nothing in the feed asked for.
    (The CalDAV backend's one-hour default is a different case: it applies to a
    genuinely absent DTEND, which the expander never hands back.)
    """
    ics = TIMED.replace("DTEND;TZID=America/New_York:20260502T100000\n", "")
    (event,) = provider(StubFeed(ics)).list_events(WINDOW)
    assert event.all_day is False
    assert event.start == datetime(2026, 5, 2, 13, 0, tzinfo=UTC)
    assert event.end == event.start


def test_a_webcal_url_is_fetched_over_https():
    """webcal:// is a subscribe scheme; on the wire it is plain https."""
    feed = StubFeed(TIMED)
    provider(feed, url=URL.replace("https://", "webcal://")).list_events(WINDOW)
    assert feed.urls == [URL]


# -- a feed can only ever be read -------------------------------------------------


def test_a_feed_holds_no_mirrors_to_find():
    """calsync can never have written to a feed, so it can never own one's events."""
    assert provider(StubFeed(MIRROR)).list_mirrors(WINDOW, "s1") == []


@pytest.mark.parametrize("write", ["create", "update", "delete"])
def test_writing_to_a_feed_is_refused(write: str):
    """A static document has nothing to write to; the config check is the first line."""
    event = CalEvent(
        uid="whatever",
        start=datetime(2026, 5, 2, 13, 0, tzinfo=UTC),
        end=datetime(2026, 5, 2, 14, 0, tzinfo=UTC),
        marker=Marker("s1", KEY, HASH),
    )
    with pytest.raises(ProviderError, match="read-only ICS feed"):
        getattr(provider(StubFeed()), write)(event)


# -- failures ---------------------------------------------------------------------


def http_error(status: int, reason: str = "nope") -> urllib.error.HTTPError:
    return urllib.error.HTTPError(url=URL, code=status, msg=reason, hdrs=None, fp=None)


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_a_server_error_is_transient(status: int):
    feed = StubFeed(error=http_error(status))
    with pytest.raises(TransientError) as excinfo:
        provider(feed).list_events(WINDOW)
    assert excinfo.value.status == status
    assert len(feed.urls) == 4  # retried, per the shared policy


@pytest.mark.parametrize("status", [401, 403, 404])
def test_a_denial_or_a_missing_feed_is_permanent(status: int):
    """Retrying a feed we may not read just burns the pass, forever."""
    feed = StubFeed(error=http_error(status))
    with pytest.raises(ProviderError) as excinfo:
        provider(feed).list_events(WINDOW)
    assert not isinstance(excinfo.value, TransientError)
    assert excinfo.value.status == status
    assert len(feed.urls) == 1


def test_a_timeout_is_transient_and_is_retried_until_it_clears():
    feed = StubFeed(TIMED, error=TimeoutError("timed out"), fail_times=2)
    (event,) = provider(feed).list_events(WINDOW)
    assert event.uid == "src-1"
    assert len(feed.urls) == 3


def test_a_timeout_that_never_clears_surfaces_as_a_transient_error():
    with pytest.raises(TransientError):
        provider(StubFeed(error=TimeoutError("timed out"))).list_events(WINDOW)


def test_an_unreachable_host_is_transient():
    with pytest.raises(TransientError):
        provider(StubFeed(error=urllib.error.URLError("nodename nor servname"))).list_events(WINDOW)


@pytest.mark.parametrize(
    "body",
    ["<html><body>Sign in to continue</body></html>", "", "BEGIN:VCALENDAR\nVERSION:2.0\n"],
    ids=["a-login-page", "empty", "truncated"],
)
def test_a_response_that_is_not_icalendar_is_permanent(body: str):
    """A feed that answers with a login page will answer that way every time."""
    feed = StubFeed(body)
    with pytest.raises(ProviderError) as excinfo:
        provider(feed).list_events(WINDOW)
    assert not isinstance(excinfo.value, TransientError)
    assert len(feed.urls) == 1


@pytest.mark.parametrize(
    ("bad", "replacement"),
    [
        ("DTSTART;TZID=America/New_York:20260502T090000", "DTSTART:not-a-timestamp"),
        ("SUMMARY:Dentist", "SUMMARY:Dentist\nRRULE:FREQ=NONSENSE"),
    ],
    ids=["undecodable-dtstart", "unusable-rrule"],
)
def test_a_malformed_event_is_reported_rather_than_crashing_the_pass(bad: str, replacement: str):
    """A bare KeyError/ValueError must not escape the backend-agnostic contract."""
    with pytest.raises(ProviderError):
        provider(StubFeed(TIMED.replace(bad, replacement))).list_events(WINDOW)


def test_an_event_that_ends_before_it_starts_is_read_as_the_period_it_spans():
    """Not the CalDAV backend's answer, and not this provider's choice either.

    CalDAV rejects a backwards VEVENT, naming the object. Expansion here repairs
    it first -- recurring_ical_events hands back the period with its ends the
    right way round -- so there is nothing left to reject by the time calsync
    sees it. Pinned because a library upgrade could quietly start dropping it.
    """
    ics = TIMED.replace("DTSTART;TZID=America/New_York:20260502T090000", "DTSTART:20260502T140000Z")
    ics = ics.replace("DTEND;TZID=America/New_York:20260502T100000", "DTEND:20260502T130000Z")
    (event,) = provider(StubFeed(ics)).list_events(WINDOW)
    assert event.start == datetime(2026, 5, 2, 13, 0, tzinfo=UTC)
    assert event.end == datetime(2026, 5, 2, 14, 0, tzinfo=UTC)


# urllib follows redirects, so the URL that appears in a failure is not always the
# one that was configured -- and a denylist built from the configured URL cannot
# remove a string it has never seen.
REDIRECT_TARGET = f"https://cdn.tripit.net/private/{TOKEN}/tripit.ics"


@pytest.mark.parametrize(
    "error",
    [
        urllib.error.URLError(f"connection to {URL} refused"),
        http_error(404, f"Not Found: {URL}"),
        TimeoutError(f"timed out reading {URL}"),
        # urllib's own refusal text quotes the *redirect target*.
        http_error(302, f"Redirection to url '{REDIRECT_TARGET}' is not allowed"),
        # The second hop's own 404, whose reason phrase repeats the redirected path.
        http_error(404, f"Not Found: /private/{TOKEN}/tripit.ics"),
        # urllib re-encodes what it puts on the wire, so the bytes in the message
        # are not byte-for-byte the bytes that were configured.
        urllib.error.URLError(f"connection to {URL.replace('.ics', '%20.ics')} refused"),
    ],
    ids=[
        "unreachable",
        "http-error",
        "timeout",
        "redirect-target",
        "redirected-path",
        "re-encoded-url",
    ],
)
def test_a_failure_names_the_host_but_never_the_secret_path(error: Exception):
    """The feed URL is a credential: TripIt's private path is the whole password.

    A message is worth nothing if it cannot say which feed failed, so the host
    stays -- but the path is exactly what must never reach a log or a report.
    """
    with pytest.raises(ProviderError) as excinfo:
        provider(StubFeed(error=error)).list_events(WINDOW)
    message = str(excinfo.value)
    assert "www.tripit.com" in message
    assert TOKEN not in message
    assert "/feed/ical/private" not in message
    assert "cdn.tripit.net" not in message
    # log.exception() prints the whole chain, so the raw text must not be chained either.
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__suppress_context__ is True


QUERY_URL = f"https://calendar.example.com/basic.ics?secret={TOKEN}"


@pytest.mark.parametrize(
    "message",
    [
        f"connection to {QUERY_URL} refused",
        # The path and query alone, with no scheme to recognise them by.
        f"GET /basic.ics?secret={TOKEN} failed",
        f"?secret={TOKEN}",
    ],
    ids=["whole-url", "path-and-query", "query-only"],
)
def test_a_secret_in_the_query_string_is_scrubbed_too(message: str):
    """Not every feed hides its token in the path; Fastmail and friends use a query."""
    feed = StubFeed(error=urllib.error.URLError(message))
    with pytest.raises(ProviderError) as excinfo:
        provider(feed, url=QUERY_URL).list_events(WINDOW)
    assert "calendar.example.com" in str(excinfo.value)
    assert TOKEN not in str(excinfo.value)


def test_scrubbed_text_is_truncated():
    """A feed controls the length of what it makes calsync quote, as well as the text."""
    assert len(scrub("x" * 10_000)) <= ics_module.MAX_DETAIL


def test_a_parse_failure_does_not_name_the_secret_path():
    with pytest.raises(ProviderError) as excinfo:
        provider(StubFeed(f"not iCalendar, and here is {URL} for good measure")).list_events(WINDOW)
    assert "www.tripit.com" in str(excinfo.value)
    assert TOKEN not in str(excinfo.value)
    # The body of a feed that answered with a login page contains the URL, so the
    # original must not ride along on the chain for log.exception() to print.
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__suppress_context__ is True


def test_ics_provider_satisfies_the_protocol():
    assert isinstance(provider(StubFeed()), Provider)


# -- the real fetcher, which never touches the network here ----------------------


class FakeResponse:
    """A urlopen response that hands its body over a chunk at a time.

    ``delay`` is what a slow-drip server does: bytes keep arriving, so no single
    socket read ever times out, and the transfer never ends.
    """

    def __init__(self, body: bytes, chunk: int = 1 << 20, delay: float = 0.0):
        self.body = body
        self.chunk = chunk
        self.delay = delay
        self.offset = 0

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, size: int = -1) -> bytes:
        if self.delay:
            time.sleep(self.delay)
        size = self.chunk if size is None or size < 0 else min(size, self.chunk)
        data = self.body[self.offset : self.offset + size]
        self.offset += len(data)
        return data


@pytest.fixture
def urlopen(monkeypatch):
    """Install a fake ``urlopen`` and hand back what the request looked like."""
    captured: dict = {}

    def install(response_factory):
        def fake_urlopen(request, timeout=None):
            captured.update(
                url=request.full_url,
                agent=request.get_header("User-agent"),
                encoding=request.get_header("Accept-encoding"),
                timeout=timeout,
            )
            return response_factory()

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        return captured

    return install


def test_the_default_fetch_names_calsync_and_asks_for_no_encoding(urlopen):
    """Python's default agent (``Python-urllib/3.x``) is refused outright by some CDNs.

    ``identity`` is asked for because a compressed body is not iCalendar, and the
    parse failure it causes is permanent -- the feed would never sync again.
    """
    captured = urlopen(lambda: FakeResponse(EMPTY.encode()))

    assert fetch_url(URL) == EMPTY.encode()
    assert captured["url"] == URL
    assert captured["agent"] == "calsync"
    assert captured["encoding"] == "identity"
    assert captured["timeout"] == ics_module.TIMEOUT


def test_a_gzipped_body_is_decompressed_rather_than_failing_to_parse(urlopen):
    """Asking for ``identity`` is a request, not a guarantee; some servers gzip anyway.

    Undetected, the gzip magic bytes are a permanent "not parseable iCalendar",
    which retries cannot clear and which no operator can diagnose from the log.
    """
    urlopen(lambda: FakeResponse(gzip.compress(EMPTY.encode())))

    assert fetch_url(URL) == EMPTY.encode()


def test_a_slow_drip_is_abandoned_when_the_total_budget_runs_out(urlopen):
    """``timeout=`` bounds one socket read, not the transfer.

    A server dripping a byte at a time resets that clock forever, and retry_call
    then multiplies the hang by four. The budget has to be wall-clock and total.
    """
    urlopen(lambda: FakeResponse(b"x" * 2_000, chunk=1, delay=0.01))

    started = time.monotonic()
    with pytest.raises(TransientError):
        fetch_url(URL, timeout=0.05)
    # Bounded by the budget, not by the body: 2,000 dripped bytes would be 20s, and
    # a real server need never stop dripping at all.
    assert time.monotonic() - started < 5.0


def test_a_slow_drip_surfaces_through_the_provider_as_transient(urlopen):
    """A server that is merely slow today may be fine on the next pass."""
    urlopen(lambda: FakeResponse(b"x" * 2_000, chunk=1, delay=0.01))
    feed = IcsProvider(URL, name="trips", fetch=lambda url: fetch_url(url, timeout=0.05))

    with pytest.raises(TransientError) as excinfo:
        feed.list_events(WINDOW)
    assert "www.tripit.com" in str(excinfo.value)
    assert TOKEN not in str(excinfo.value)


def test_an_oversized_feed_is_refused_rather_than_buffered(urlopen, monkeypatch):
    """415 MB of RSS for a feed with nothing in the window is not a sync, it is a leak."""
    monkeypatch.setattr(ics_module, "MAX_FEED_BYTES", 64)
    urlopen(lambda: FakeResponse(b"x" * 4096, chunk=16))

    with pytest.raises(ProviderError) as excinfo:
        fetch_url(URL)
    assert "64" in str(excinfo.value)
    # Retrying cannot make the feed smaller.
    assert not isinstance(excinfo.value, TransientError)
    assert TOKEN not in str(excinfo.value)


def test_an_oversized_feed_names_the_host_through_the_provider(urlopen, monkeypatch):
    """The fetcher has no safe way to name the feed, so the provider adds the host."""
    monkeypatch.setattr(ics_module, "MAX_FEED_BYTES", 64)
    urlopen(lambda: FakeResponse(b"x" * 4096, chunk=16))
    feed = IcsProvider(URL, name="trips", fetch=fetch_url)

    with pytest.raises(ProviderError) as excinfo:
        feed.list_events(WINDOW)
    assert "www.tripit.com" in str(excinfo.value)
    assert "64" in str(excinfo.value)
    assert TOKEN not in str(excinfo.value)
