"""Diff desired mirrors against what the destination already has, and apply."""

from __future__ import annotations

import dataclasses
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime

from calsync.config import SyncSpec
from calsync.model import CalEvent
from calsync.providers.base import Provider
from calsync.transform import desired_mirrors, skip_reason

log = logging.getLogger(__name__)


class DeleteRailTripped(Exception):
    """A pass wanted to delete more mirrors than the sync permits."""


@dataclass(frozen=True)
class Operations:
    create: list[CalEvent] = field(default_factory=list)
    update: list[CalEvent] = field(default_factory=list)
    delete: list[CalEvent] = field(default_factory=list)


@dataclass(frozen=True)
class SyncResult:
    sync_id: str
    created: int = 0
    updated: int = 0
    deleted: int = 0
    skipped: int = 0
    dry_run: bool = False
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    def summary(self) -> str:
        if self.error:
            return f"{self.sync_id}: FAILED {self.error}"
        prefix = "would " if self.dry_run else ""
        return (
            f"{self.sync_id}: {prefix}create={self.created} {prefix}update={self.updated} "
            f"{prefix}delete={self.deleted} skipped={self.skipped}"
        )


def group_mirrors(
    mirrors: list[CalEvent], sync_id: str
) -> tuple[dict[str, CalEvent], list[CalEvent]]:
    """This sync's mirrors indexed by calsync key, plus the duplicates to reap.

    A key should be unique on the destination, but a provider retry after a
    failure that actually committed, or two calsync instances racing, can leave
    two mirrors sharing one. Keeping the first and returning the rest as extras
    makes a duplicate reapable instead of permanently invisible.

    Mirrors stamped by another sync are dropped: only the fake provider enforces
    the ``sync_id`` filter itself, and a backend that over-returns must not let
    another sync's mirror look destination-only and be deleted.
    """
    kept: dict[str, CalEvent] = {}
    extras: list[CalEvent] = []
    for mirror in mirrors:
        if not mirror.is_mirror or mirror.marker.sync_id != sync_id:
            continue
        if mirror.marker.key in kept:
            extras.append(mirror)
        else:
            kept[mirror.marker.key] = mirror
    return kept, extras


def by_key(mirrors: list[CalEvent], sync_id: str) -> dict[str, CalEvent]:
    """This sync's mirrors indexed by calsync key, one mirror per key."""
    return group_mirrors(mirrors, sync_id)[0]


def diff(
    desired: dict[str, CalEvent],
    existing: dict[str, CalEvent],
    extras: Sequence[CalEvent] = (),
) -> Operations:
    """Compare by calsync key; an unknown stored hash always forces an update.

    ``extras`` are duplicate mirrors of a key that is kept in ``existing``; they
    are deleted so the destination converges on one mirror per key.
    """
    create = [event for key, event in desired.items() if key not in existing]
    update = [
        # Address the destination's own event (native id plus any backend
        # addressing detail in ``raw``, such as a CalDAV href), but carry the
        # desired content.
        dataclasses.replace(desired[key], uid=existing[key].uid, raw=existing[key].raw)
        for key in desired
        if key in existing and existing[key].marker.hash != desired[key].marker.hash
    ]
    delete = [event for key, event in existing.items() if key not in desired]
    delete.extend(extras)
    return Operations(create=create, update=update, delete=delete)


def run_sync(
    spec: SyncSpec,
    source: Provider,
    dest: Provider,
    now: datetime,
    dry_run: bool = False,
) -> SyncResult:
    window = spec.window.bounds(now)
    events = source.list_events(window)
    desired = desired_mirrors(events, spec)
    existing, duplicates = group_mirrors(dest.list_mirrors(window, spec.id), spec.id)
    ops = diff(desired, existing, duplicates)

    if len(ops.delete) > spec.max_deletes_per_pass:
        raise DeleteRailTripped(
            f"sync '{spec.id}' wanted {len(ops.delete)} deletions, "
            f"limit is {spec.max_deletes_per_pass}; refusing to apply anything"
        )

    if not dry_run:
        applied = {"create": 0, "update": 0, "delete": 0}
        try:
            # Create and update before deleting: a moved event is a create plus a
            # delete of the old mirror, and deleting first would show the user as
            # free for the window between the two calls.
            for event in ops.create:
                dest.create(event)
                applied["create"] += 1
            for event in ops.update:
                dest.update(event)
                applied["update"] += 1
            for event in ops.delete:
                dest.delete(event)
                applied["delete"] += 1
        except Exception:
            # This result is discarded as the failure propagates, so record what
            # did land -- the destination has already been written to.
            log.error(
                "sync '%s' failed part-way through apply: "
                "created=%d updated=%d deleted=%d already applied",
                spec.id,
                applied["create"],
                applied["update"],
                applied["delete"],
            )
            raise

    return SyncResult(
        sync_id=spec.id,
        created=len(ops.create),
        updated=len(ops.update),
        deleted=len(ops.delete),
        skipped=sum(1 for event in events if skip_reason(event, spec.skip) is not None),
        dry_run=dry_run,
    )
