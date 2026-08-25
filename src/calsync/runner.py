"""Run every configured sync, once or on an interval."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from calsync.config import Account, CalendarRef, Config
from calsync.providers.base import Provider
from calsync.sync import SyncResult, run_sync

log = logging.getLogger(__name__)

DEFAULT_HEARTBEAT = Path("/tmp/calsync-heartbeat")

ProviderFactory = Callable[[str, Account, str], Provider]


def run_pass(
    config: Config,
    factory: ProviderFactory,
    now: datetime,
    dry_run: bool = False,
) -> list[SyncResult]:
    """Run every sync once. A failing sync is recorded, never propagated."""
    cache: dict[tuple[str, str], Provider] = {}

    def provider_for(ref: CalendarRef) -> Provider:
        key = (ref.account, ref.calendar)
        if key not in cache:
            cache[key] = factory(ref.account, config.accounts[ref.account], ref.calendar)
        return cache[key]

    results: list[SyncResult] = []
    try:
        for spec in config.syncs:
            try:
                result = run_sync(
                    spec, provider_for(spec.source), provider_for(spec.dest), now, dry_run
                )
            except Exception as exc:  # noqa: BLE001 - isolation is the whole point
                log.exception("sync '%s' failed", spec.id)
                result = SyncResult(sync_id=spec.id, error=f"{type(exc).__name__}: {exc}")
            log.info("%s", result.summary())
            results.append(result)
    finally:
        close_all(cache.values())
    return results


def close_all(providers: Iterable[Provider]) -> None:
    """Release every provider that holds a connection, best effort.

    Providers are built fresh each pass, so a session nobody closes is a leak
    that grows with uptime. Closing happens after all the useful work, so a
    provider that refuses to hang up is logged and stepped over: failing the
    pass on it would withhold the heartbeat and mark the container unhealthy
    over syncs that all succeeded. Providers without a ``close`` -- Google's
    client keeps no session of its own -- are simply skipped.
    """
    for provider in providers:
        close = getattr(provider, "close", None)
        if close is None:
            continue
        try:
            close()
        except Exception:  # noqa: BLE001 - a failed hang-up cannot fail the pass
            log.warning(
                "could not close provider '%s'", getattr(provider, "name", provider), exc_info=True
            )


def record_heartbeat(heartbeat: Path, results: list[SyncResult]) -> None:
    """Touch the heartbeat only for a wholly successful pass, and log why not.

    The container healthcheck reads this file, so a permanently broken sync has
    to keep the container unhealthy -- but the reason must be visible at the
    point of the decision. A heartbeat that cannot be written (read-only mount,
    missing parent) is logged too, never raised: it would crash-loop a container
    whose syncs are working.
    """
    if not results:
        log.warning("heartbeat not touched: the pass produced no results")
        return
    failed = [result.sync_id for result in results if not result.ok]
    if failed:
        log.warning("heartbeat not touched: sync(s) failed: %s", ", ".join(failed))
        return
    try:
        heartbeat.touch()
    except OSError as exc:
        log.error("could not touch the heartbeat at %s: %s", heartbeat, exc)


def run_forever(
    config: Config,
    factory: ProviderFactory,
    interval: timedelta,
    heartbeat: Path = DEFAULT_HEARTBEAT,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    sleep: Callable[[float], None] = time.sleep,
    passes: int | None = None,
) -> None:
    """Sync on an interval. ``passes`` bounds the loop; ``None`` means forever."""
    completed = 0
    while passes is None or completed < passes:
        results = run_pass(config, factory, now=clock())
        record_heartbeat(heartbeat, results)
        completed += 1
        # Sleep between passes only: after the final bounded pass there is
        # nothing left to wait for.
        if passes is None or completed < passes:
            sleep(interval.total_seconds())
