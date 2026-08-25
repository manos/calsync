"""The contract every calendar backend implements."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from calsync.model import CalEvent

Window = tuple[datetime, datetime]


class ProviderError(Exception):
    """A backend rejected an operation and retrying will not help.

    ``status`` carries the backend's HTTP status when the failure had one, so
    callers can discriminate (``delete`` tolerates 404/410) without matching on
    the message text -- which contains the request URI, and so can contain a
    "404" that belongs to an event id rather than to the response.
    """

    def __init__(self, message: str = "", *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class TransientError(ProviderError):
    """A backend failed in a way that is worth retrying (429, 5xx, timeout)."""


@runtime_checkable
class Provider(Protocol):
    """Read and write one calendar.

    ``uid`` on a returned event is the backend-native identifier; callers pass an
    event straight back to ``update``/``delete`` to address it.
    """

    name: str

    def list_events(self, window: Window) -> list[CalEvent]:
        """All occurrences overlapping the window, recurrences already expanded.

        The window is half-open: an event that ends exactly at its start, or
        starts exactly at its end, is not in it. Results are ordered by start
        time (Google asks for ``orderBy=startTime``).
        """
        ...

    def list_mirrors(self, window: Window, sync_id: str) -> list[CalEvent]:
        """Events in the window that calsync created for ``sync_id``.

        Same window rule as ``list_events``: the sync id narrows that result,
        it does not widen it.
        """
        ...

    def create(self, event: CalEvent) -> None:
        """Write a new event.

        Raises ProviderError if ``event.marker`` is None. calsync only ever
        writes mirrors it can recognise again later, so an unmarked write is a
        bug that would leak an untracked event onto a real calendar. The
        backend may assign its own ``uid``, so callers must not assume the one
        they passed in is the one now stored.
        """
        ...

    def update(self, event: CalEvent) -> None:
        """Overwrite the event that ``event.uid`` addresses.

        Raises ProviderError if that event no longer exists -- unlike a delete,
        a missing target means the caller's view of the calendar is stale and
        the intended write cannot be expressed. Also raises, as ``create``
        does, if ``event.marker`` is None.
        """
        ...

    def delete(self, event: CalEvent) -> None:
        """Remove the event that ``event.uid`` addresses.

        Deleting an event that is already gone succeeds: the desired end state
        already holds. Implementations swallow the backend's not-found rather
        than raise.
        """
        ...
