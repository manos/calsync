"""Deterministic identity for mirrored events.

calsync keeps no state, so a mirror must carry enough information to be matched
back to its source on the next pass. The key is derived from inputs that are
stable across passes; the hash captures everything a change should trigger an
update for.
"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime

from calsync.model import CalEvent

KEY_RE = r"[0-9a-f]{16}"
HASH_RE = r"[0-9a-f]{16}"

# Identity lives in two independent channels, so losing one does not lose the mirror:
#
# * the UID, ``calsync-<sync_id>-<key>@calsync``, which is the object's identity on
#   every CalDAV server and survives anything short of a re-create;
# * the description line, ``[calsync:<key>:<hash>]``, which survives a server that
#   drops unknown properties (iCloud drops X- properties on some calendars).
#
# The backend-native properties (Google's extendedProperties, the X-CALSYNC-* iCal
# properties) are preferred when present, but neither channel above depends on them.
# Carrying only the key in the fallback used to make a mirror invisible to its own
# sync once the properties went missing: every pass re-created it and none was ever
# reaped, so the calendar filled with duplicates.

# Anchored to a whole line: calsync always writes the marker on its own line, and a
# marker quoted inside a user's prose must not be read as calsync identity (that would
# classify their event as a mirror and silently never sync it). The optional \r absorbs
# the CRLF that CalDAV round-trips can hand back.
MARKER_PATTERN = re.compile(
    rf"^[ \t]*\[calsync:({KEY_RE}):({HASH_RE})\][ \t]*\r?$\n?", re.MULTILINE
)

# The sync id is constrained in config.py to ``[A-Za-z0-9][A-Za-z0-9_-]*``, so it may
# contain hyphens; the key is fixed-width hex, which makes the last hyphen-separated
# component unambiguously the key however many hyphens precede it.
MIRROR_UID_PATTERN = re.compile(rf"^calsync-([A-Za-z0-9][A-Za-z0-9_-]*)-({KEY_RE})@calsync$")

PROP_SYNC = "calsync_sync"
PROP_KEY = "calsync_key"
PROP_HASH = "calsync_hash"

ICAL_PROP_SYNC = "X-CALSYNC-SYNC"
ICAL_PROP_KEY = "X-CALSYNC-KEY"
ICAL_PROP_HASH = "X-CALSYNC-HASH"


def _digest(*parts: str) -> str:
    return hashlib.sha256("\x00".join(parts).encode()).hexdigest()[:16]


def mirror_key(sync_id: str, source_uid: str, instance_start: datetime) -> str:
    """Stable identity of the mirror for one source occurrence."""
    if instance_start.tzinfo is None or instance_start.utcoffset() is None:
        # A naive value would key off whatever local zone the host happens to be in,
        # making the join key host- and DST-dependent.
        raise ValueError(f"instance_start must be timezone-aware, got {instance_start!r}")
    return _digest(sync_id, source_uid, instance_start.astimezone(UTC).isoformat())


def mirror_uid(sync_id: str, key: str) -> str:
    """UID written on mirrors, carrying both halves of the mirror's identity."""
    return f"calsync-{sync_id}-{key}@calsync"


def parse_mirror_uid(uid: str | None) -> tuple[str, str] | None:
    """Recover ``(sync_id, key)`` from a calsync UID, or ``None`` if it is not one."""
    match = MIRROR_UID_PATTERN.match(uid or "")
    return (match.group(1), match.group(2)) if match else None


def content_hash(event: CalEvent) -> str:
    """Hash of the fields that make a mirror stale when they change."""
    return _digest(
        event.start.astimezone(UTC).isoformat(),
        event.end.astimezone(UTC).isoformat(),
        event.title,
        strip_marker(event.description),
        event.location,
        "all_day" if event.all_day else "timed",
    )


def marker_line(key: str, hash: str) -> str:
    return f"[calsync:{key}:{hash}]"


def parse_marker_line(description: str | None) -> tuple[str, str] | None:
    """Recover ``(key, hash)`` from a description's marker line, if it has one."""
    match = MARKER_PATTERN.search(description or "")
    return (match.group(1), match.group(2)) if match else None


def parse_key(description: str | None) -> str | None:
    parsed = parse_marker_line(description)
    return parsed[0] if parsed else None


def strip_marker(description: str | None) -> str:
    return MARKER_PATTERN.sub("", description or "").strip()


def stamp_description(description: str, key: str, hash: str) -> str:
    """Append the marker line to a description, replacing any existing one."""
    body = strip_marker(description)
    return f"{body}\n\n{marker_line(key, hash)}" if body else marker_line(key, hash)
