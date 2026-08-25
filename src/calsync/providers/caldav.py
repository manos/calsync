"""CalDAV provider, exercised against iCloud."""

from __future__ import annotations

import datetime as dt
from typing import Any

from caldav.davclient import requests as dav_http
from caldav.lib import error as dav_error
from icalendar import Calendar as ICalendar
from icalendar import Event as IEvent

from calsync.marker import (
    ICAL_PROP_HASH,
    ICAL_PROP_KEY,
    ICAL_PROP_SYNC,
    mirror_uid,
    stamp_description,
    strip_marker,
)
from calsync.model import CalEvent
from calsync.providers.base import ProviderError, TransientError, Window
from calsync.providers.ical import read_marker, span, text
from calsync.retry import retry_call

TRANSIENT_STATUSES = {429, 500, 502, 503, 504}
UTC_MIDNIGHT = dt.time.min.replace(tzinfo=dt.UTC)
# ``caldav.davclient`` aliases whichever HTTP library it actually loaded -- niquests
# when it is installed, requests otherwise -- so binding to the alias catches the
# transport failures caldav can really raise. Importing ``requests`` directly instead
# caught nothing: niquests' exceptions do not subclass requests', so a connection
# timeout escaped unwrapped and unretried through the backend-agnostic contract.
NETWORK_ERRORS = (dav_http.ConnectionError, dav_http.Timeout)


def _status(exc: dav_error.DAVError) -> int | None:
    """The HTTP status behind a caldav error, when it carries one.

    caldav builds most of these as ``DAVError(errmsg(response))``, and ``errmsg``
    formats ``"<status> <reason>\\n\\n<body>"`` into the ``url`` slot -- so the status
    is the leading token there. Reading it positionally, rather than searching the
    whole message, keeps a "503" inside a response body or an event href from being
    mistaken for the response's own status.

    ``AuthorizationError`` is the exception: caldav raises it for a real 401/403 with
    the request URL in that slot, not errmsg output, so it has no status to read and
    has to be recognised by type.
    """
    if isinstance(exc, dav_error.AuthorizationError):
        return 403
    head = str(getattr(exc, "url", "") or "").split(None, 1)
    if head and head[0].isdigit():
        return int(head[0])
    return None


def _dav_call(func: Any) -> Any:
    """Run a CalDAV operation, classifying failures as transient or permanent."""

    def call() -> Any:
        try:
            return func()
        except dav_error.NotFoundError:
            # Raised unwrapped so delete() can read "already gone" as success.
            raise
        except NETWORK_ERRORS as exc:
            raise TransientError(str(exc)) from exc
        except dav_error.DAVError as exc:
            status = _status(exc)
            # caldav raises RateLimitError for 429 and for 503-with-Retry-After; it
            # carries the request URL rather than errmsg output, so it has no status
            # to read and has to be recognised by type.
            if isinstance(exc, dav_error.RateLimitError) or status in TRANSIENT_STATUSES:
                raise TransientError(str(exc), status=status) from exc
            raise ProviderError(str(exc), status=status) from exc

    return retry_call(call)


class CaldavProvider:
    """Read and write one CalDAV calendar.

    The calendar arrives already connected; discovery is the caller's job.

    Identity is split across two fields. ``uid`` is the VEVENT UID, which is
    stable across passes, while the object's href goes in ``raw["href"]``
    because the href is what addresses it for update and delete. The differ
    copies ``raw`` from the existing mirror onto the desired content, so an
    update always has an href to write to.

    On a mirror the UID is not merely stable, it is load-bearing: calsync writes
    ``calsync-<sync_id>-<key>@calsync`` there so that a server which drops the
    X-CALSYNC-* properties -- iCloud does, on some calendars -- cannot hide a
    mirror from its own sync. See ``_read_marker``.

    CalDAV mirrors are never recurring -- calsync writes one standalone VEVENT
    per occurrence -- so one href always holds exactly one mirror.
    """

    def __init__(self, calendar: Any, email: str, name: str = "caldav") -> None:
        self._calendar = calendar
        self._email = email.lower()
        self.name = name

    # -- reading -------------------------------------------------------------

    def list_events(self, window: Window) -> list[CalEvent]:
        start, end = window
        objects = _dav_call(
            lambda: self._calendar.search(start=start, end=end, event=True, expand=True)
        )
        events: list[CalEvent] = []
        for obj in objects:
            events.extend(self._to_events(obj))
        return events

    def list_mirrors(self, window: Window, sync_id: str) -> list[CalEvent]:
        return [
            event
            for event in self.list_events(window)
            if event.is_mirror and event.marker.sync_id == sync_id
        ]

    def _to_events(self, obj: Any) -> list[CalEvent]:
        """Parse one CalDAV object, naming it if it turns out to be malformed.

        A single unparseable VEVENT -- no DTSTART, a DTEND before it, a value
        icalendar cannot decode -- must not escape as a bare KeyError/ValueError
        through the backend-agnostic Provider contract and take the whole pass
        down with an error that names nothing.
        """
        href = str(obj.url)
        try:
            calendar = ICalendar.from_ical(obj.data)
        except (KeyError, ValueError, TypeError) as exc:
            raise ProviderError(f"CalDAV object {href} is not parseable iCalendar: {exc}") from exc
        events: list[CalEvent] = []
        for component in calendar.walk("VEVENT"):
            try:
                events.append(_to_event(component, href, self._email))
            except (KeyError, ValueError, TypeError) as exc:
                raise ProviderError(f"CalDAV event in {href}: {exc}") from exc
        return events

    # -- writing -------------------------------------------------------------

    def create(self, event: CalEvent) -> None:
        ical = _to_ical(event)
        _dav_call(lambda: self._calendar.save_event(ical))

    def update(self, event: CalEvent) -> None:
        href = event.raw.get("href")
        if not href:
            raise ProviderError("cannot update a CalDAV event without an href")
        ical = _to_ical(event)
        try:
            obj = _dav_call(lambda: self._calendar.event_by_url(href))
        except dav_error.NotFoundError as exc:
            # _dav_call leaves NotFoundError unwrapped for delete()'s benefit. Here it
            # has to be translated, or a caldav-native exception escapes through the
            # backend-agnostic Provider contract, which promises a ProviderError.
            raise ProviderError(f"CalDAV event {href} is gone: {exc}", status=404) from exc
        obj.data = ical
        _dav_call(obj.save)

    def delete(self, event: CalEvent) -> None:
        href = event.raw.get("href")
        if not href:
            raise ProviderError("cannot delete a CalDAV event without an href")
        try:
            obj = _dav_call(lambda: self._calendar.event_by_url(href))
        except dav_error.NotFoundError:
            return  # Already gone is the state we wanted.
        _dav_call(obj.delete)

    # -- lifecycle -----------------------------------------------------------

    def close(self) -> None:
        """Release the HTTP session this calendar was discovered through.

        The runner rebuilds every provider once per pass, so an unclosed
        DAVClient leaks a connection pool per CalDAV calendar per pass -- about
        a hundred a day at the default interval.

        The client is read off the calendar rather than passed in separately:
        caldav hangs the discovering DAVClient off every object it returns
        (``DAVObject.client``), and a second copy handed in by the factory could
        disagree with the one the calendar actually talks through.
        ``DAVClient.close()`` closes its ``session``, which is the real owner.
        """
        client = getattr(self._calendar, "client", None)
        if client is not None:
            client.close()


def _at_utc_midnight(moment: dt.datetime) -> bool:
    # CalEvent normalises to UTC, so comparing the time of day is a UTC comparison.
    return moment.timetz() == UTC_MIDNIGHT


def _to_event(component: IEvent, href: str, email: str) -> CalEvent:
    start, end, all_day = span(component)
    uid = text(component, "uid")
    description = text(component, "description")
    marker = read_marker(component, uid, description)

    return CalEvent(
        uid=uid,
        start=start,
        end=end,
        title=text(component, "summary"),
        description=strip_marker(description),
        location=text(component, "location"),
        all_day=all_day,
        cancelled=text(component, "status").upper() == "CANCELLED",
        declined=_self_declined(component, email),
        transparent=text(component, "transp").upper() == "TRANSPARENT",
        marker=marker,
        raw={"href": href},
    )


def _self_declined(component: IEvent, email: str) -> bool:
    """Whether *this user* declined, which is the only decline worth skipping on.

    Without an email there is no way to tell the user's RSVP from a colleague's, so
    nothing is treated as declined: over-mirroring is recoverable, silently dropping
    the user's own event because someone else declined it is not.
    """
    if not email:
        return False
    attendees = component.get("attendee")
    if attendees is None:
        return False
    if not isinstance(attendees, list):
        attendees = [attendees]
    for attendee in attendees:
        if str(attendee.params.get("PARTSTAT", "")).upper() != "DECLINED":
            continue
        if email in _attendee_addresses(attendee):
            return True
    return False


def _attendee_addresses(attendee: Any) -> set[str]:
    """Every address an ATTENDEE line identifies its participant by, lowercased.

    The value is usually ``mailto:``, but iCloud and CalendarServer routinely write
    a principal URI there and put the real address in the ``EMAIL`` parameter --
    matching only on ``mailto:`` misses the user's own RSVP on exactly those servers.
    """
    candidates = (str(attendee), str(attendee.params.get("EMAIL", "")))
    return {value.strip().lower().removeprefix("mailto:") for value in candidates if value}


def _to_ical(event: CalEvent) -> str:
    marker = event.marker
    if marker is None:
        raise ProviderError("refusing to write an event without a calsync marker")

    component = IEvent()
    component.add("uid", mirror_uid(marker.sync_id, marker.key))
    component.add("dtstamp", dt.datetime.now(dt.UTC))
    if event.all_day and _at_utc_midnight(event.start) and _at_utc_midnight(event.end):
        # DATE-valued, the way an all-day event is meant to be written. As a timed
        # block at UTC midnight instead, it renders a day early west of Greenwich and
        # marks the user busy from the previous evening.
        component.add("dtstart", event.start.date())
        component.add("dtend", event.end.date())
    else:
        # Padding moved an edge off midnight; the DATE form cannot express that, and
        # dropping it silently would be worse than keeping the timed block.
        component.add("dtstart", event.start)
        component.add("dtend", event.end)
    component.add("summary", event.title)
    component.add("description", stamp_description(event.description, marker.key, marker.hash))
    if event.location:
        component.add("location", event.location)
    # A transparent mirror would reserve no time at all, which is the whole point.
    component.add("transp", "OPAQUE")
    component.add(ICAL_PROP_SYNC, marker.sync_id)
    component.add(ICAL_PROP_KEY, marker.key)
    component.add(ICAL_PROP_HASH, marker.hash)

    calendar = ICalendar()
    calendar.add("prodid", "-//calsync//EN")
    calendar.add("version", "2.0")
    calendar.add_component(component)
    return calendar.to_ical().decode()
