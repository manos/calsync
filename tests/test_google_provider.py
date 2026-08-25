import functools
import json
from datetime import UTC, datetime

import pytest
from googleapiclient.errors import HttpError

import calsync.providers.google as google_module
from calsync.model import CalEvent, Marker
from calsync.providers.base import Provider, ProviderError, TransientError
from calsync.providers.google import GoogleProvider, _to_body
from calsync.retry import retry_call

WINDOW = (datetime(2026, 5, 1, tzinfo=UTC), datetime(2026, 5, 15, tzinfo=UTC))
# Deliberately not "primary": a hardcoded default would otherwise pass every assertion.
CALENDAR_ID = "dest@group.calendar.google.com"
HASH = "abcdef0123456789"


@pytest.fixture(autouse=True)
def no_backoff_sleep(monkeypatch):
    """Keep the real retry policy but stop it sleeping through the test suite."""
    monkeypatch.setattr(
        google_module, "retry_call", functools.partial(retry_call, sleep=lambda _: None)
    )


class StubRequest:
    def __init__(self, result=None, error: Exception | None = None, fail_times: int | None = None):
        self.result = result if result is not None else {"items": []}
        self.error = error
        # None means "fail every time"; an int makes the request flaky for that many calls.
        self.fail_times = fail_times
        self.executions = 0

    def execute(self):
        self.executions += 1
        if self.error and (self.fail_times is None or self.executions <= self.fail_times):
            raise self.error
        return self.result


class StubEvents:
    def __init__(
        self,
        pages: list[dict] | None = None,
        error: Exception | None = None,
        fail_times: int | None = None,
    ):
        self.pages = pages or [{"items": []}]
        self.error = error
        self.fail_times = fail_times
        self.calls: list[tuple[str, dict]] = []
        self.requests: list[StubRequest] = []

    def _request(self, result) -> StubRequest:
        request = StubRequest(result, error=self.error, fail_times=self.fail_times)
        self.requests.append(request)
        return request

    def list(self, **kwargs):
        self.calls.append(("list", kwargs))
        index = 1 if kwargs.get("pageToken") else 0
        return self._request(self.pages[min(index, len(self.pages) - 1)])

    def insert(self, **kwargs):
        self.calls.append(("insert", kwargs))
        return self._request({"id": "created-id"})

    def update(self, **kwargs):
        self.calls.append(("update", kwargs))
        return self._request({"id": kwargs.get("eventId")})

    def delete(self, **kwargs):
        self.calls.append(("delete", kwargs))
        return self._request({})


class StubService:
    def __init__(self, events: StubEvents):
        self._events = events

    def events(self):
        return self._events


def error_body(reason: str, message: str, code: int) -> bytes:
    """The shape Google actually returns: the reason lives inside error.errors."""
    return json.dumps(
        {
            "error": {
                "errors": [{"domain": "global", "reason": reason, "message": message}],
                "code": code,
                "message": message,
            }
        }
    ).encode()


RATE_LIMIT_BODY = error_body("rateLimitExceeded", "Rate Limit Exceeded", 403)
USER_RATE_LIMIT_BODY = error_body("userRateLimitExceeded", "User Rate Limit Exceeded", 403)
PERMISSION_BODY = error_body("forbidden", "Forbidden", 403)


def http_error(status: int, content: bytes = b'{"error": {"message": "stub"}}', uri=None):
    class Resp:
        def __init__(self, status):
            self.status = status
            self.reason = "stub"

    return HttpError(Resp(status), content, uri=uri)


def provider(events: StubEvents) -> GoogleProvider:
    return GoogleProvider(StubService(events), calendar_id=CALENDAR_ID, name="work")


def api_event(**overrides) -> dict:
    base = {
        "id": "gid-1",
        "summary": "Dentist",
        "description": "bring card",
        "location": "12 Main St",
        "start": {"dateTime": "2026-05-02T09:00:00-04:00"},
        "end": {"dateTime": "2026-05-02T10:00:00-04:00"},
        "status": "confirmed",
    }
    return {**base, **overrides}


def mirror(**overrides) -> CalEvent:
    base = {
        "uid": "calsync-s1-0123456789abcdef@calsync",
        "start": datetime(2026, 5, 2, 13, 0, tzinfo=UTC),
        "end": datetime(2026, 5, 2, 14, 0, tzinfo=UTC),
        "title": "Busy",
        "marker": Marker("s1", "0123456789abcdef", HASH),
    }
    return CalEvent(**{**base, **overrides})


def test_parses_a_timed_event_into_utc():
    p = provider(StubEvents([{"items": [api_event()]}]))
    (event,) = p.list_events(WINDOW)
    assert event.uid == "gid-1"
    assert event.title == "Dentist"
    assert event.start == datetime(2026, 5, 2, 13, 0, tzinfo=UTC)
    assert event.end == datetime(2026, 5, 2, 14, 0, tzinfo=UTC)
    assert event.all_day is False


def test_parses_an_all_day_event():
    p = provider(
        StubEvents(
            [{"items": [api_event(start={"date": "2026-05-02"}, end={"date": "2026-05-03"})]}]
        )
    )
    (event,) = p.list_events(WINDOW)
    assert event.all_day is True
    assert event.start == datetime(2026, 5, 2, tzinfo=UTC)
    assert event.end == datetime(2026, 5, 3, tzinfo=UTC)


def test_parses_status_transparency_and_own_rsvp():
    p = provider(
        StubEvents(
            [
                {
                    "items": [
                        api_event(id="a", status="cancelled"),
                        api_event(id="b", transparency="transparent"),
                        api_event(
                            id="c",
                            attendees=[
                                {"email": "other@x.com", "responseStatus": "declined"},
                                {"email": "me@x.com", "self": True, "responseStatus": "declined"},
                            ],
                        ),
                        api_event(
                            id="d",
                            attendees=[
                                {"email": "me@x.com", "self": True, "responseStatus": "accepted"}
                            ],
                        ),
                    ]
                }
            ]
        )
    )
    events = {e.uid: e for e in p.list_events(WINDOW)}
    assert events["a"].cancelled is True
    assert events["b"].transparent is True
    assert events["c"].declined is True
    assert events["d"].declined is False


def test_an_ordinary_event_is_neither_transparent_nor_cancelled():
    """The defaults matter: a busy event read as free would never be mirrored."""
    p = provider(
        StubEvents([{"items": [api_event(id="plain"), api_event(id="rsvp", status="tentative")]}])
    )
    events = {e.uid: e for e in p.list_events(WINDOW)}
    assert events["plain"].transparent is False
    assert events["plain"].cancelled is False
    # An unanswered invite is not a cancellation.
    assert events["rsvp"].cancelled is False


def test_only_the_self_attendees_decline_counts():
    """Someone else declining must not drop the user's own event."""
    p = provider(
        StubEvents(
            [
                {
                    "items": [
                        api_event(
                            attendees=[
                                {"email": "other@x.com", "responseStatus": "declined"},
                                {"email": "me@x.com", "self": True, "responseStatus": "accepted"},
                            ],
                        )
                    ]
                }
            ]
        )
    )
    (event,) = p.list_events(WINDOW)
    assert event.declined is False


@pytest.mark.parametrize("event_type", ["workingLocation", "birthday", "fromGmail"])
def test_event_types_that_do_not_block_time_are_read_as_free(event_type: str):
    """These are calendar decoration; the default skip.free filter drops them."""
    p = provider(StubEvents([{"items": [api_event(eventType=event_type)]}]))
    (event,) = p.list_events(WINDOW)
    assert event.transparent is True


@pytest.mark.parametrize("event_type", ["focusTime", "outOfOffice", "default"])
def test_event_types_that_do_block_time_stay_busy(event_type: str):
    p = provider(StubEvents([{"items": [api_event(eventType=event_type)]}]))
    (event,) = p.list_events(WINDOW)
    assert event.transparent is False


def test_reads_the_marker_from_extended_properties_and_strips_it_from_the_description():
    p = provider(
        StubEvents(
            [
                {
                    "items": [
                        api_event(
                            description=f"notes\n\n[calsync:0123456789abcdef:{HASH}]",
                            extendedProperties={
                                "private": {
                                    "calsync_sync": "s1",
                                    "calsync_key": "0123456789abcdef",
                                    "calsync_hash": HASH,
                                }
                            },
                        )
                    ]
                }
            ]
        )
    )
    (event,) = p.list_events(WINDOW)
    assert event.marker == Marker(sync_id="s1", key="0123456789abcdef", hash=HASH)
    assert event.description == "notes"


def test_falls_back_to_the_description_marker_when_properties_are_missing():
    p = provider(
        StubEvents([{"items": [api_event(description=f"[calsync:0123456789abcdef:{HASH}]")]}])
    )
    (event,) = p.list_events(WINDOW)
    assert event.marker.key == "0123456789abcdef"
    # The line carries the hash too, so a mirror whose properties went missing is
    # not needlessly rewritten on every pass.
    assert event.marker.hash == HASH


def test_the_private_property_wins_over_a_stale_description_marker():
    """The property is what calsync wrote last; prose can be edited by anyone."""
    p = provider(
        StubEvents(
            [
                {
                    "items": [
                        api_event(
                            description="notes\n\n[calsync:ffffffffffffffff:0000000000000000]",
                            extendedProperties={
                                "private": {
                                    "calsync_sync": "s1",
                                    "calsync_key": "0123456789abcdef",
                                    "calsync_hash": HASH,
                                }
                            },
                        )
                    ]
                }
            ]
        )
    )
    (event,) = p.list_events(WINDOW)
    assert event.marker.key == "0123456789abcdef"


def test_a_malformed_endpoint_is_reported_as_a_provider_error_naming_the_event():
    """One bad item must not kill the pass with a bare KeyError."""
    p = provider(StubEvents([{"items": [api_event(id="gid-broken", start={}, end={})]}]))
    with pytest.raises(ProviderError) as excinfo:
        p.list_events(WINDOW)
    assert "gid-broken" in str(excinfo.value)


def test_an_item_without_an_id_is_reported_as_a_provider_error():
    item = api_event()
    del item["id"]
    with pytest.raises(ProviderError):
        provider(StubEvents([{"items": [item]}])).list_events(WINDOW)


def test_list_events_expands_recurrences_and_follows_pagination():
    events = StubEvents(
        [
            {"items": [api_event(id="p1")], "nextPageToken": "tok"},
            {"items": [api_event(id="p2")]},
        ]
    )
    p = provider(events)
    assert [e.uid for e in p.list_events(WINDOW)] == ["p1", "p2"]

    _, kwargs = events.calls[0]
    assert kwargs["calendarId"] == CALENDAR_ID
    assert kwargs["singleEvents"] is True
    assert kwargs["timeMin"] == "2026-05-01T00:00:00+00:00"
    assert kwargs["timeMax"] == "2026-05-15T00:00:00+00:00"
    assert events.calls[1][1]["pageToken"] == "tok"
    assert events.calls[1][1]["calendarId"] == CALENDAR_ID


def test_list_asks_for_ordered_live_events_only():
    """showDeleted is load-bearing: cancelled instances come back with no start/end."""
    events = StubEvents()
    provider(events).list_events(WINDOW)
    _, kwargs = events.calls[0]
    assert kwargs["showDeleted"] is False
    assert kwargs["orderBy"] == "startTime"


def test_list_mirrors_filters_server_side_by_sync_id():
    events = StubEvents()
    provider(events).list_mirrors(WINDOW, "s1")
    _, kwargs = events.calls[0]
    assert kwargs["calendarId"] == CALENDAR_ID
    assert kwargs["privateExtendedProperty"] == "calsync_sync=s1"


def test_create_serializes_marker_properties_and_suppresses_reminders():
    events = StubEvents()
    provider(events).create(mirror(description="bring card", location="12 Main St"))

    method, kwargs = events.calls[0]
    assert method == "insert"
    assert kwargs["calendarId"] == CALENDAR_ID
    body = kwargs["body"]
    assert body["summary"] == "Busy"
    assert body["start"] == {"dateTime": "2026-05-02T13:00:00+00:00", "timeZone": "UTC"}
    assert body["description"] == f"bring card\n\n[calsync:0123456789abcdef:{HASH}]"
    assert body["location"] == "12 Main St"
    # A transparent mirror would reserve no time at all, which is the whole point.
    assert body["transparency"] == "opaque"
    assert body["extendedProperties"]["private"] == {
        "calsync_sync": "s1",
        "calsync_key": "0123456789abcdef",
        "calsync_hash": HASH,
    }
    assert body["reminders"] == {"useDefault": False}


def test_an_all_day_mirror_is_written_in_googles_date_form():
    """A timed body at UTC midnight renders a day early west of Greenwich."""
    body = _to_body(
        mirror(
            start=datetime(2026, 5, 2, tzinfo=UTC),
            end=datetime(2026, 5, 4, tzinfo=UTC),
            all_day=True,
        )
    )
    # end.date is exclusive, which is how _parse_endpoint reads it back.
    assert body["start"] == {"date": "2026-05-02"}
    assert body["end"] == {"date": "2026-05-04"}


def test_a_written_all_day_mirror_reads_back_as_the_same_all_day_event():
    source = mirror(
        start=datetime(2026, 5, 2, tzinfo=UTC), end=datetime(2026, 5, 4, tzinfo=UTC), all_day=True
    )
    stored = {**_to_body(source), "id": "gid-new"}
    (read_back,) = provider(StubEvents([{"items": [stored]}])).list_events(WINDOW)
    assert read_back.all_day is True
    assert (read_back.start, read_back.end) == (source.start, source.end)


def test_a_padded_all_day_mirror_keeps_the_timed_form():
    """Padding moved the edges off midnight; the date form would silently drop it."""
    body = _to_body(
        mirror(
            start=datetime(2026, 5, 1, 23, 30, tzinfo=UTC),
            end=datetime(2026, 5, 4, 0, 30, tzinfo=UTC),
            all_day=True,
        )
    )
    assert body["start"] == {"dateTime": "2026-05-01T23:30:00+00:00", "timeZone": "UTC"}
    assert body["end"] == {"dateTime": "2026-05-04T00:30:00+00:00", "timeZone": "UTC"}


def test_update_addresses_the_native_event_id():
    events = StubEvents()
    provider(events).update(mirror(uid="gid-99", marker=Marker("s1", "0123456789abcdef", "f" * 16)))
    method, kwargs = events.calls[0]
    assert method == "update"
    assert kwargs["eventId"] == "gid-99"
    assert kwargs["calendarId"] == CALENDAR_ID


@pytest.mark.parametrize("write", ["create", "update"])
def test_writing_an_unmarked_event_is_refused(write: str):
    """calsync never leaves an untracked event on a real calendar."""
    events = StubEvents()
    unmarked = CalEvent(
        uid="gid-99",
        start=datetime(2026, 5, 2, 13, 0, tzinfo=UTC),
        end=datetime(2026, 5, 2, 14, 0, tzinfo=UTC),
        title="Busy",
    )
    with pytest.raises(ProviderError):
        getattr(provider(events), write)(unmarked)
    assert events.calls == []


@pytest.mark.parametrize("status", [404, 410])
def test_delete_tolerates_an_already_deleted_event(status: int):
    events = StubEvents(error=http_error(status))
    provider(events).delete(mirror(uid="gid-99"))  # must not raise
    method, kwargs = events.calls[0]
    assert method == "delete"
    assert kwargs == {"calendarId": CALENDAR_ID, "eventId": "gid-99"}


def test_delete_does_not_swallow_a_permission_failure():
    """A denied delete that reports success makes a broken pass look healthy."""
    # The request URI contains "404": matching on message text would swallow this.
    events = StubEvents(
        error=http_error(403, PERMISSION_BODY, uri="https://www.googleapis.com/events/ev404x")
    )
    with pytest.raises(ProviderError):
        provider(events).delete(mirror(uid="ev404x"))


def test_delete_does_not_swallow_a_transient_failure():
    events = StubEvents(error=http_error(503))
    with pytest.raises(TransientError):
        provider(events).delete(mirror(uid="gid-99"))


def test_server_errors_become_transient_and_client_errors_do_not():
    with pytest.raises(TransientError):
        provider(StubEvents(error=http_error(503))).list_events(WINDOW)
    with pytest.raises(TransientError):
        provider(StubEvents(error=http_error(429))).list_events(WINDOW)
    with pytest.raises(ProviderError) as excinfo:
        provider(StubEvents(error=http_error(400))).list_events(WINDOW)
    assert not isinstance(excinfo.value, TransientError)


def test_a_permission_denied_403_is_permanent():
    """Retrying a calendar we are not allowed to read just burns the pass, forever."""
    events = StubEvents(error=http_error(403, PERMISSION_BODY))
    with pytest.raises(ProviderError) as excinfo:
        provider(events).list_events(WINDOW)
    assert not isinstance(excinfo.value, TransientError)
    assert events.requests[0].executions == 1


@pytest.mark.parametrize("body", [RATE_LIMIT_BODY, USER_RATE_LIMIT_BODY], ids=["rate", "user-rate"])
def test_a_rate_limited_403_is_transient(body: bytes):
    with pytest.raises(TransientError):
        provider(StubEvents(error=http_error(403, body))).list_events(WINDOW)


def test_a_transient_failure_is_retried_until_it_succeeds():
    events = StubEvents([{"items": [api_event()]}], error=http_error(503), fail_times=2)
    (event,) = provider(events).list_events(WINDOW)
    assert event.uid == "gid-1"
    assert events.requests[0].executions == 3


@pytest.mark.parametrize(
    ("title", "description"),
    [("Busy", ""), ("Dentist", "bring card")],
    ids=["busy-mirror", "detail-mirror"],
)
def test_a_written_mirror_reads_back_with_the_same_marker(title: str, description: str):
    """The marker must survive the write/read round trip, or the loop guard fails."""
    source = mirror(title=title, description=description)
    # Google assigns the id; everything else is what we sent.
    stored = {**_to_body(source), "id": "gid-new"}

    (read_back,) = provider(StubEvents([{"items": [stored]}])).list_events(WINDOW)

    assert read_back.marker == source.marker
    assert read_back.title == source.title
    assert read_back.description == source.description
    assert (read_back.start, read_back.end) == (source.start, source.end)


def test_google_provider_satisfies_the_protocol():
    assert isinstance(provider(StubEvents()), Provider)
