"""Normalized, backend-agnostic calendar types."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any


@dataclass(frozen=True)
class Marker:
    """Identity calsync stamps onto every mirror it creates.

    ``hash`` may be empty when a backend dropped the stored hash; callers treat
    an empty hash as "unknown", which forces an update.
    """

    sync_id: str
    key: str
    hash: str = ""


@dataclass(frozen=True)
class CalEvent:
    """A single calendar occurrence, with times always in UTC.

    ``uid`` is the backend-native identifier of the event, used to address it for
    updates and deletes. It is not the calsync key; that lives in ``marker``.

    An all-day event is represented as UTC midnight standing in for a floating
    date: ``all_day`` is true and ``start``/``end`` are midnight *in UTC*, not
    midnight in any local zone. Providers must not use local midnight, or the
    same day would compare unequal across timezones.

    ``travel_before`` is lead time the source calendar records *separately* from
    the event's own span -- Apple Calendar's ``X-APPLE-TRAVEL-DURATION``, the
    drive to get there. It is not part of ``start``/``end``: the event still
    begins at ``start``, and the user is merely already in the car before that.
    Only a source event carries it; ``transform`` folds it into the mirror's
    start, so a mirror's own lead time is always zero.

    ``raw`` carries backend addressing detail (the CalDAV provider stores the
    object's ``href`` there, for instance) that is not part of the event's
    identity or content. It is excluded from equality and hashing, so two events
    that differ only in backend detail compare equal.
    """

    uid: str
    start: datetime
    end: datetime
    title: str = ""
    description: str = ""
    location: str = ""
    all_day: bool = False
    travel_before: timedelta = timedelta(0)
    cancelled: bool = False
    declined: bool = False
    transparent: bool = False
    marker: Marker | None = None
    raw: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)

    def __post_init__(self) -> None:
        for name in ("start", "end"):
            value = getattr(self, name)
            # datetime subclasses date, so this rejects only plain dates -- which is
            # what icalendar hands back for all-day events.
            if not isinstance(value, datetime):
                raise ValueError(f"{name} must be a datetime, got {type(value).__name__}")
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError(f"{name} must be timezone-aware, got {value!r} (uid={self.uid!r})")
            object.__setattr__(self, name, value.astimezone(UTC))
        if self.end < self.start:
            raise ValueError(
                f"end before start: start={self.start.isoformat()}, "
                f"end={self.end.isoformat()} (uid={self.uid!r})"
            )
        if self.travel_before < timedelta(0):
            # Negative lead time would move a mirror's start forward, past the
            # moment the event itself begins, and shrink the block it reserves.
            raise ValueError(
                f"travel_before must not be negative, got {self.travel_before} (uid={self.uid!r})"
            )

    @property
    def is_mirror(self) -> bool:
        return self.marker is not None
