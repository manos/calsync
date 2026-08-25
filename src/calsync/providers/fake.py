"""In-memory Provider implementation used by tests and dry-run experiments."""

from __future__ import annotations

import dataclasses
from collections.abc import Callable

from calsync.model import CalEvent
from calsync.providers.base import ProviderError, Window


def overlaps(event: CalEvent, window: Window) -> bool:
    start, end = window
    return event.end > start and event.start < end


def _detached(event: CalEvent) -> CalEvent:
    """A copy safe to hand out: mutating its ``raw`` cannot reach the store."""
    return dataclasses.replace(event, raw=dict(event.raw))


def _require_marker(event: CalEvent) -> None:
    if event.marker is None:
        raise ProviderError("refusing to write an event without a calsync marker")


class FakeProvider:
    """A dict-backed Provider that mimics what the real backends do.

    ``assign_uid`` simulates a backend that ignores the uid the client supplies
    and hands back its own, the way Google does on create; left as None the
    supplied uid is kept. ``calls`` records the uid an operation actually
    addressed, so under ``assign_uid`` a create records the assigned uid.
    """

    def __init__(
        self,
        events: list[CalEvent] | None = None,
        name: str = "fake",
        assign_uid: Callable[[CalEvent], str] | None = None,
    ) -> None:
        self.name = name
        self.events: dict[str, CalEvent] = {event.uid: event for event in events or []}
        self.calls: list[tuple[str, str]] = []
        self._assign_uid = assign_uid

    def list_events(self, window: Window) -> list[CalEvent]:
        return sorted(
            (_detached(event) for event in self.events.values() if overlaps(event, window)),
            key=lambda event: (event.start, event.uid),
        )

    def list_mirrors(self, window: Window, sync_id: str) -> list[CalEvent]:
        return [
            event
            for event in self.list_events(window)
            if event.is_mirror and event.marker.sync_id == sync_id
        ]

    def create(self, event: CalEvent) -> None:
        uid = self._assign_uid(event) if self._assign_uid is not None else event.uid
        self.calls.append(("create", uid))
        _require_marker(event)
        if uid in self.events:
            raise ProviderError(f"event {uid} already exists")
        self.events[uid] = dataclasses.replace(event, uid=uid)

    def update(self, event: CalEvent) -> None:
        self.calls.append(("update", event.uid))
        _require_marker(event)
        if event.uid not in self.events:
            raise ProviderError(f"event {event.uid} not found")
        self.events[event.uid] = event

    def delete(self, event: CalEvent) -> None:
        # Already gone is the state we wanted, and it is what both real
        # providers do with the backend's not-found.
        self.calls.append(("delete", event.uid))
        self.events.pop(event.uid, None)
