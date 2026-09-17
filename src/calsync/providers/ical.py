"""iCalendar reading shared by the backends that speak it.

CalDAV serves iCalendar objects and an ICS feed is one iCalendar document, so
both backends decode the same properties into the same ``CalEvent`` fields. The
rules live here once: two providers that disagreed about what a DTSTART means
would key the same occurrence differently and mirror it twice.
"""

from __future__ import annotations

import datetime as dt

from icalendar import Event as IEvent
from icalendar import vDuration

from calsync.marker import (
    ICAL_PROP_HASH,
    ICAL_PROP_KEY,
    ICAL_PROP_SYNC,
    parse_marker_line,
    parse_mirror_uid,
)
from calsync.model import Marker

# Apple Calendar's travel time. It is lead time -- the journey *to* the event -- and
# Apple records no counterpart for the journey home, so nothing here invents one.
APPLE_TRAVEL_DURATION = "X-APPLE-TRAVEL-DURATION"
# The longest run of calendar-controlled text quoted back in a failure message.
MAX_VALUE_DETAIL = 50


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


def _as_duration(value: object) -> dt.timedelta | None:
    """One property value as a timedelta, or ``None`` if it does not hold one."""
    try:
        decoded = value.dt
    except AttributeError:
        # No VALUE=DURATION parameter, so icalendar had no type to decode against and
        # left the raw text alone. Servers that re-serialise an event do drop it.
        decoded = None
    except ValueError:
        # icalendar decoded it against VALUE=DURATION and rejected what it found.
        decoded = None
    if decoded is not None:
        # Decoded as something that is not a duration at all -- a DATE-TIME, say --
        # is a malformed property, not text to have another go at.
        return decoded if isinstance(decoded, dt.timedelta) else None
    try:
        return vDuration.from_ical(str(value))
    except ValueError:
        return None


def travel_before(component: IEvent, uid: str) -> dt.timedelta:
    """Lead time the calendar records apart from the event's own span.

    Apple Calendar stores the journey to an event in ``X-APPLE-TRAVEL-DURATION``
    and leaves DTSTART alone, so an event read from its span alone shows the user
    free while they are actually still driving. There is no counterpart for the
    journey home -- Apple records none -- so this is lead time only.

    Google Calendar has no equivalent: travel time there is a Maps feature of the
    UI, not data on the event, which is why this lives in the iCalendar reader.
    """
    value = component.get(APPLE_TRAVEL_DURATION)
    if value is None:
        return dt.timedelta(0)
    duration = _as_duration(value)
    if duration is None:
        # Reported rather than ignored, for the same reason a DTEND that will not
        # decode is: silently mirroring a block that is missing an hour and a half
        # of the user's afternoon is worse than naming the event that broke.
        raise ValueError(
            f"{APPLE_TRAVEL_DURATION} is not a duration: "
            f"{str(value)[:MAX_VALUE_DETAIL]!r} (uid={uid!r})"
        )
    # Zero means "no travel"; a negative value cannot describe a journey at all, and
    # CalEvent rejects one outright. Both say the same thing here.
    return max(duration, dt.timedelta(0))


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
