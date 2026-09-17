"""Turn source events into the mirrors that should exist on the destination."""

from __future__ import annotations

import dataclasses
from datetime import timedelta

from calsync.config import SkipSpec, SyncSpec
from calsync.marker import content_hash, mirror_key, mirror_uid, strip_marker
from calsync.model import CalEvent, Marker


def skip_reason(event: CalEvent, skip: SkipSpec) -> str | None:
    """Why this event should not be mirrored, or ``None`` to mirror it."""
    # Safe to trust on a third-party ICS feed too: a marker read out of a source can
    # only suppress an event here, never select one for deletion -- deletions come
    # from list_mirrors, and an ICS provider's is always empty.
    if skip.mirrors and event.is_mirror:
        return "mirror"
    if skip.cancelled and event.cancelled:
        return "cancelled"
    if skip.declined and event.declined:
        return "declined"
    if skip.free and event.transparent:
        return "free"
    if skip.all_day and event.all_day:
        return "all_day"
    return None


def lead_time(event: CalEvent, spec: SyncSpec) -> timedelta:
    """How far before its source an event's mirror should start.

    Configured padding and the source's own travel time stack: the padding is a
    buffer the user asked for around every event, and the travel time is the drive
    their calendar already knows about. Both are time they are not free.

    Travel time is dropped on an all-day source. A lead time on a whole-day block
    says nothing useful, and subtracting it would move the start off midnight --
    which is exactly what forces the all-day form to a timed one.
    """
    travel = timedelta(0) if event.all_day else event.travel_before
    return spec.padding.before + travel


def build_mirror(event: CalEvent, spec: SyncSpec) -> CalEvent:
    """Construct the mirror a single source event should produce."""
    key = mirror_key(spec.id, event.uid, event.start)
    busy = spec.privacy == "busy"
    mirror = CalEvent(
        uid=mirror_uid(spec.id, key),
        # The end takes padding only: travel time is the journey *to* the event, and
        # no source records a journey home for calsync to mirror.
        start=event.start - lead_time(event, spec),
        end=event.end + spec.padding.after,
        title=spec.title if busy else event.title,
        description="" if busy else strip_marker(event.description),
        location="" if busy else event.location,
        all_day=False if busy else event.all_day,
    )
    marker = Marker(sync_id=spec.id, key=key, hash=content_hash(mirror))
    return dataclasses.replace(mirror, marker=marker)


def desired_mirrors(events: list[CalEvent], spec: SyncSpec) -> dict[str, CalEvent]:
    """The complete set of mirrors this sync wants on the destination, by key.

    Two source events sharing a uid and start collapse to one entry (last wins),
    which is the correct dedup for a backend that returns the same occurrence twice.
    """
    desired: dict[str, CalEvent] = {}
    for event in events:
        if skip_reason(event, spec.skip) is not None:
            continue
        mirror = build_mirror(event, spec)
        desired[mirror.marker.key] = mirror
    return desired
