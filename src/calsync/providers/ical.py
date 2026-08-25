"""iCalendar reading shared by the backends that speak it.

CalDAV serves iCalendar objects and an ICS feed is one iCalendar document, so
both backends decode the same properties into the same ``CalEvent`` fields. The
rules live here once: two providers that disagreed about what a DTSTART means
would key the same occurrence differently and mirror it twice.
"""

from __future__ import annotations

import datetime as dt

from icalendar import Event as IEvent

from calsync.marker import (
    ICAL_PROP_HASH,
    ICAL_PROP_KEY,
    ICAL_PROP_SYNC,
    parse_marker_line,
    parse_mirror_uid,
)
from calsync.model import Marker


def as_utc(value: dt.datetime | dt.date) -> tuple[dt.datetime, bool]:
    """One DTSTART/DTEND value in UTC, and whether it was a bare date."""
    if isinstance(value, dt.datetime):
        if value.tzinfo is None:
            # A floating (timezone-less) time is deliberately read as UTC. RFC 5545
            # says it means "local time wherever the event is viewed", which calsync
            # has no way to resolve: it keeps no state and does not know the
            # calendar's zone. Reading it as the host's local zone would instead make
            # the mirror key host- and DST-dependent, so the same occurrence would
            # rekey -- delete and re-create -- whenever the host moved. UTC is wrong
            # by a fixed offset; local is wrong unpredictably.
            value = value.replace(tzinfo=dt.UTC)
        return value.astimezone(dt.UTC), False
    return dt.datetime.combine(value, dt.time.min, tzinfo=dt.UTC), True


def text(component: IEvent, name: str) -> str:
    value = component.get(name)
    return str(value) if value is not None else ""


def span(component: IEvent) -> tuple[dt.datetime, dt.datetime, bool]:
    """Start, end and all-day-ness of one VEVENT, in UTC."""
    start, all_day = as_utc(component.decoded("dtstart"))
    if "dtend" in component:
        end, _ = as_utc(component.decoded("dtend"))
    elif "duration" in component:
        end = start + component.decoded("duration")
    else:
        # RFC 5545 3.6.1: a DATE-valued DTSTART with neither DTEND nor DURATION lasts
        # one day; only a DATE-TIME one defaults to an hour. Reading a bare all-day
        # event as an hour long would mirror it as an hour of busy time.
        end = start + (dt.timedelta(days=1) if all_day else dt.timedelta(hours=1))
    return start, end, all_day


def read_marker(component: IEvent, uid: str, description: str) -> Marker | None:
    """Identity from the X- properties, falling back per field to the other channels.

    The properties are what calsync wrote last, so they win. Below them sit two
    channels that do not depend on the server preserving unknown properties: the
    UID, which carries the sync id and the key, and the description's marker line,
    which carries the key and the hash.

    The UID outranks the marker line for the key because a UID is the object's
    identity on every CalDAV server, while a description is prose a user can edit.
    """
    from_uid = parse_mirror_uid(uid)
    from_line = parse_marker_line(description)

    key = text(component, ICAL_PROP_KEY) or (from_uid[1] if from_uid else "")
    if not key:
        key = from_line[0] if from_line else ""
    if not key:
        return None
    return Marker(
        sync_id=text(component, ICAL_PROP_SYNC) or (from_uid[0] if from_uid else ""),
        key=key,
        # An empty hash means "unknown", which forces an update -- far cheaper than
        # the alternative of not recognising the mirror at all and duplicating it.
        hash=text(component, ICAL_PROP_HASH) or (from_line[1] if from_line else ""),
    )
