from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from calsync.config import SYNC_ID_PATTERN
from calsync.marker import (
    content_hash,
    marker_line,
    mirror_key,
    mirror_uid,
    parse_key,
    parse_marker_line,
    parse_mirror_uid,
    stamp_description,
    strip_marker,
)
from calsync.model import CalEvent

HASH = "abcdef0123456789"


def event(**kwargs) -> CalEvent:
    base = {
        "uid": "src-1",
        "start": datetime(2026, 5, 1, 9, 0, tzinfo=UTC),
        "end": datetime(2026, 5, 1, 10, 0, tzinfo=UTC),
        "title": "Busy",
    }
    return CalEvent(**{**base, **kwargs})


def test_mirror_key_is_stable_and_16_hex_chars():
    start = datetime(2026, 5, 1, 9, 0, tzinfo=UTC)
    key = mirror_key("personal-to-work", "src-1", start)
    assert key == mirror_key("personal-to-work", "src-1", start)
    assert len(key) == 16
    assert all(c in "0123456789abcdef" for c in key)


def test_mirror_key_ignores_source_timezone_representation():
    utc = datetime(2026, 5, 1, 13, 0, tzinfo=UTC)
    eastern = datetime(2026, 5, 1, 9, 0, tzinfo=ZoneInfo("America/New_York"))
    assert mirror_key("s", "src-1", utc) == mirror_key("s", "src-1", eastern)


def test_mirror_key_varies_with_each_input():
    start = datetime(2026, 5, 1, 9, 0, tzinfo=UTC)
    other = datetime(2026, 5, 2, 9, 0, tzinfo=UTC)
    keys = {
        mirror_key("s1", "src-1", start),
        mirror_key("s2", "src-1", start),
        mirror_key("s1", "src-2", start),
        mirror_key("s1", "src-1", other),
    }
    assert len(keys) == 4


def test_mirror_key_distinguishes_adjacent_field_boundaries():
    start = datetime(2026, 5, 1, 9, 0, tzinfo=UTC)
    assert mirror_key("ab", "c", start) != mirror_key("a", "bc", start)


def test_mirror_key_rejects_naive_start():
    with pytest.raises(ValueError):
        mirror_key("s", "src-1", datetime(2026, 5, 1, 9, 0))


def test_mirror_uid_embeds_the_sync_id_and_the_key():
    assert mirror_uid("s1", "0123456789abcdef") == "calsync-s1-0123456789abcdef@calsync"


def test_mirror_uid_round_trips_through_its_parser():
    # The UID is the identity channel that survives a server stripping custom
    # properties, so both fields have to come back out of it intact.
    assert parse_mirror_uid(mirror_uid("s1", "0123456789abcdef")) == ("s1", "0123456789abcdef")


@pytest.mark.parametrize(
    "sync_id", ["personal-to-work", "work_to_shared", "Sync1", "0a", "s", "a-b-c_d-e"]
)
def test_every_sync_id_config_accepts_round_trips_through_a_uid(sync_id: str):
    # config.py constrains the sync id to exactly this alphabet. The key is the last
    # hyphen-separated component, so hyphens in the id do not make the split ambiguous.
    assert SYNC_ID_PATTERN.match(sync_id)
    assert parse_mirror_uid(mirror_uid(sync_id, "0123456789abcdef")) == (
        sync_id,
        "0123456789abcdef",
    )


def test_mirror_uid_parser_takes_the_last_component_as_the_key():
    """A sync id that itself ends in something key-shaped still splits correctly."""
    uid = mirror_uid("x-0123456789abcdef", "fedcba9876543210")
    assert parse_mirror_uid(uid) == ("x-0123456789abcdef", "fedcba9876543210")


@pytest.mark.parametrize(
    "uid",
    [
        "src-1",
        "",
        "calsync-0123456789abcdef@calsync",  # no sync id
        "calsync-s1-0123456789abcde@calsync",  # key too short
        "calsync-s1-0123456789abcdefa@calsync",  # key too long
        "calsync-s1-0123456789abcdeg@calsync",  # key not hex
        "calsync-s1-0123456789abcdef@example.com",  # not our domain
        "calsync--0123456789abcdef@calsync",  # empty sync id
        "prefix-calsync-s1-0123456789abcdef@calsync",
    ],
)
def test_parse_mirror_uid_rejects_anything_calsync_did_not_write(uid: str):
    """A foreign UID must never be read as calsync identity, or we would mirror it."""
    assert parse_mirror_uid(uid) is None


def test_content_hash_changes_with_content():
    baseline = content_hash(event())
    assert content_hash(event()) == baseline
    assert content_hash(event(title="Other")) != baseline
    assert (
        content_hash(
            event(
                start=datetime(2026, 5, 1, 8, 0, tzinfo=UTC),
                end=datetime(2026, 5, 1, 10, 0, tzinfo=UTC),
            )
        )
        != baseline
    )
    assert content_hash(event(end=datetime(2026, 5, 1, 11, 0, tzinfo=UTC))) != baseline
    assert content_hash(event(description="notes")) != baseline
    assert content_hash(event(location="Room 2")) != baseline
    assert content_hash(event(all_day=True)) != baseline


def test_content_hash_ignores_uid_and_marker_line():
    baseline = content_hash(event())
    assert content_hash(event(uid="different")) == baseline
    assert content_hash(
        event(description=f"notes\n[calsync:0123456789abcdef:{HASH}]")
    ) == content_hash(event(description="notes"))


def test_marker_line_carries_the_key_and_the_hash():
    assert marker_line("0123456789abcdef", HASH) == f"[calsync:0123456789abcdef:{HASH}]"


def test_marker_line_round_trips():
    line = marker_line("0123456789abcdef", HASH)
    assert parse_key(f"notes here\n{line}") == "0123456789abcdef"
    assert parse_key("no marker here") is None


def test_parse_marker_line_recovers_both_the_key_and_the_hash():
    """The hash in the line is what saves a mirror from a pointless update."""
    line = marker_line("0123456789abcdef", HASH)
    assert parse_marker_line(f"notes here\n{line}") == ("0123456789abcdef", HASH)
    assert parse_marker_line("no marker here") is None


def test_marker_quoted_in_user_text_is_not_identity():
    """A marker pasted mid-sentence is the user's prose, not calsync's stamp.

    calsync always writes the marker on a line of its own, so anything embedded in
    running text must be left alone -- otherwise a user's event would be mistaken
    for a mirror and silently never synced.
    """
    text = f"we agreed [calsync:0123456789abcdef:{HASH}] means a mirror"
    assert parse_key(text) is None
    assert parse_marker_line(text) is None
    assert strip_marker(text) == text
    own_line = f"we agreed\n{marker_line('0123456789abcdef', HASH)}"
    assert parse_key(own_line) == "0123456789abcdef"
    assert strip_marker(own_line) == "we agreed"


@pytest.mark.parametrize(
    "line",
    [
        f"[calsync:0123456789abcde:{HASH}]",  # key too short
        f"[calsync:0123456789abcdef0:{HASH}]",  # key too long
        f"[calsync:0123456789abcdeg:{HASH}]",  # key not hex
        "[calsync:0123456789abcdef]",  # no hash at all
        "[calsync:0123456789abcdef:abcdef012345678]",  # hash too short
        "[calsync:0123456789abcdef:abcdef012345678g]",  # hash not hex
    ],
)
def test_parse_rejects_malformed_marker_lines(line: str):
    assert parse_key(line) is None
    assert parse_marker_line(line) is None


def test_strip_marker_removes_line_and_trailing_whitespace():
    assert strip_marker(f"notes\n\n[calsync:0123456789abcdef:{HASH}]") == "notes"
    assert strip_marker(f"[calsync:0123456789abcdef:{HASH}]") == ""
    assert strip_marker("plain notes") == "plain notes"


def test_stamp_description_is_idempotent():
    once = stamp_description("notes", "0123456789abcdef", HASH)
    assert once == f"notes\n\n[calsync:0123456789abcdef:{HASH}]"
    assert stamp_description(once, "0123456789abcdef", HASH) == once
    assert stamp_description("", "0123456789abcdef", HASH) == f"[calsync:0123456789abcdef:{HASH}]"


def test_stamp_description_replaces_a_stale_marker_rather_than_appending():
    """An update rewrites the line, so the hash there tracks the current content."""
    stale = stamp_description("notes", "0123456789abcdef", "0" * 16)
    fresh = stamp_description(stale, "0123456789abcdef", HASH)
    assert fresh == f"notes\n\n[calsync:0123456789abcdef:{HASH}]"
    assert parse_marker_line(fresh) == ("0123456789abcdef", HASH)
