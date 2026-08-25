"""Exponential backoff for backend calls that are worth retrying."""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable

from calsync.providers.base import TransientError

log = logging.getLogger(__name__)


def retry_call[T](
    func: Callable[[], T],
    *,
    attempts: int = 4,
    base_delay: float = 1.0,
    sleep: Callable[[float], None] = time.sleep,
    jitter: Callable[[], float] = random.random,
) -> T:
    """Call ``func``, retrying only on :class:`TransientError`."""
    for attempt in range(1, attempts + 1):
        try:
            return func()
        except TransientError as exc:
            if attempt == attempts:
                raise
            delay = base_delay * (2 ** (attempt - 1)) * (1 + jitter())
            log.warning(
                "transient failure (%s), retry %d/%d in %.1fs", exc, attempt, attempts, delay
            )
            sleep(delay)
    raise AssertionError("unreachable")
