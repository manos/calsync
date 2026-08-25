from datetime import UTC, datetime, timedelta

import pytest

from calsync.config import (
    ConfigError,
    _merge,
    load_config,
    load_config_file,
    parse_duration,
)

MINIMAL = """
accounts:
  home:
    type: caldav
    url: https://caldav.icloud.com
    username: ${ICLOUD_USER}
    password: ${ICLOUD_PASS}
  work:
    type: google
    client_id: cid
    client_secret: csec
    refresh_token: rtok

defaults:
  padding: {before: 15m, after: 15m}
  max_deletes_per_pass: 5

syncs:
  - id: personal-to-work
    source: {account: home, calendar: Home}
    dest: {account: work, calendar: primary}
  - id: work-to-shared
    source: {account: work, calendar: primary}
    dest: {account: home, calendar: Shared}
    privacy: full
    padding: {before: 0m, after: 0m}
"""

ENV = {"ICLOUD_USER": "me@example.com", "ICLOUD_PASS": "pw#with:specials"}

# A config with no ``defaults:`` block, so model defaults are observable.
BARE = """
accounts:
  home:
    type: caldav
    url: https://caldav.icloud.com
    username: user
    password: pass

syncs:
  - id: only
    source: {account: home, calendar: Home}
    dest: {account: home, calendar: Mirror}
"""


# A read-only feed mirrored into a real calendar: the only shape an ics account has.
ICS = """
accounts:
  trips:
    type: ics
    url: https://www.tripit.com/feed/ical/private/not-a-real-token/tripit.ics
  home:
    type: caldav
    url: https://caldav.icloud.com
    username: user
    password: pass

syncs:
  - id: trips-to-home
    source: {account: trips, calendar: ignored}
    dest: {account: home, calendar: Mirror}
"""


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("30s", timedelta(seconds=30)),
        ("15m", timedelta(minutes=15)),
        ("2h", timedelta(hours=2)),
        ("14d", timedelta(days=14)),
        ("0m", timedelta(0)),
    ],
)
def test_parse_duration(text, expected):
    assert parse_duration(text) == expected


def test_parse_duration_rejects_garbage():
    with pytest.raises(ValueError, match="invalid duration"):
        parse_duration("soon")


@pytest.mark.parametrize("value", ["-15m", -900, -1.5, timedelta(minutes=-15)])
def test_parse_duration_rejects_negative_durations(value):
    # A negative window inverts start/end; negative padding shrinks events.
    with pytest.raises(ValueError):
        parse_duration(value)


def test_env_interpolation_handles_special_characters():
    config = load_config(MINIMAL, env=ENV)
    account = config.accounts["home"]
    assert account.username == "me@example.com"
    assert account.password == "pw#with:specials"


def test_missing_env_var_is_an_error():
    with pytest.raises(ConfigError, match="ICLOUD_PASS"):
        load_config(MINIMAL, env={"ICLOUD_USER": "me@example.com"})


def test_defaults_apply_and_syncs_override_them():
    config = load_config(MINIMAL, env=ENV)
    first, second = config.syncs
    assert first.padding.before == timedelta(minutes=15)
    assert first.max_deletes_per_pass == 5
    assert first.privacy == "busy"
    assert first.title == "Busy"
    assert second.padding.before == timedelta(0)
    assert second.privacy == "full"
    assert second.max_deletes_per_pass == 5


def test_window_defaults_to_one_day_back_and_two_weeks_forward():
    config = load_config(MINIMAL, env=ENV)
    window = config.syncs[0].window
    assert window.past == timedelta(days=1)
    assert window.future == timedelta(days=14)
    start, end = window.bounds(datetime(2026, 5, 1, 12, 0, tzinfo=UTC))
    assert start == datetime(2026, 4, 30, 12, 0, tzinfo=UTC)
    assert end == datetime(2026, 5, 15, 12, 0, tzinfo=UTC)


def test_unknown_account_reference_is_an_error():
    text = MINIMAL.replace("account: home, calendar: Home", "account: nope, calendar: Home")
    with pytest.raises(ConfigError, match="unknown account 'nope'"):
        load_config(text, env=ENV)


def test_duplicate_sync_ids_are_an_error():
    text = MINIMAL.replace("id: work-to-shared", "id: personal-to-work")
    with pytest.raises(ConfigError, match="duplicate sync id"):
        load_config(text, env=ENV)


def test_typo_in_a_key_is_rejected():
    text = MINIMAL.replace("privacy: full", "privcy: full")
    with pytest.raises(ConfigError, match="privcy"):
        load_config(text, env=ENV)


def test_unknown_dest_account_reference_is_an_error():
    text = MINIMAL.replace("account: work, calendar: primary}", "account: nope, calendar: primary}")
    with pytest.raises(ConfigError, match="dest references unknown account 'nope'"):
        load_config(text, env=ENV)


def test_validation_errors_do_not_leak_interpolated_secrets():
    fake_password = "fake-app-password-not-a-real-secret"
    text = MINIMAL.replace("password: ${ICLOUD_PASS}", "passwrd: ${ICLOUD_PASS}")
    env = dict(ENV, ICLOUD_PASS=fake_password)
    with pytest.raises(ConfigError) as excinfo:
        load_config(text, env=env)
    message = str(excinfo.value)
    assert "passwrd" in message
    assert fake_password not in message


def test_sync_from_a_calendar_to_itself_is_rejected():
    text = BARE.replace("calendar: Mirror}", "calendar: Home}")
    with pytest.raises(ConfigError, match="sync 'only' has the same source and dest"):
        load_config(text, env=ENV)


def test_sync_between_two_calendars_of_one_account_is_allowed():
    config = load_config(BARE, env=ENV)
    assert config.syncs[0].source.account == config.syncs[0].dest.account


@pytest.mark.parametrize("entry", ["  - oops", "  -"])
def test_malformed_sync_entries_are_rejected(entry):
    # Silently dropping these would leave a calendar the user believes is syncing absent.
    text = BARE + entry + "\n"
    with pytest.raises(ConfigError):
        load_config(text, env=ENV)


def test_non_mapping_defaults_is_a_config_error():
    text = BARE.replace("syncs:", "defaults: oops\n\nsyncs:")
    with pytest.raises(ConfigError, match="defaults"):
        load_config(text, env=ENV)


def test_missing_syncs_key_is_an_error():
    text = BARE.split("syncs:")[0]
    with pytest.raises(ConfigError, match="syncs"):
        load_config(text, env=ENV)


def test_empty_syncs_list_is_an_error():
    text = BARE.split("syncs:")[0] + "syncs: []\n"
    with pytest.raises(ConfigError, match="syncs"):
        load_config(text, env=ENV)


def test_skip_flags_default_to_true():
    skip = load_config(MINIMAL, env=ENV).syncs[0].skip
    assert (skip.declined, skip.free, skip.all_day, skip.cancelled, skip.mirrors) == (
        True,
        True,
        True,
        True,
        True,
    )


def test_max_deletes_per_pass_defaults_to_twenty():
    assert load_config(BARE, env=ENV).syncs[0].max_deletes_per_pass == 20


def test_negative_max_deletes_per_pass_is_rejected():
    text = BARE.replace(
        "dest: {account: home", "max_deletes_per_pass: -1\n    dest: {account: home"
    )
    with pytest.raises(ConfigError, match="max_deletes_per_pass"):
        load_config(text, env=ENV)


@pytest.mark.parametrize(
    "sync_id",
    ["only-1", "only_1", "Only1", "o", "0a", "a-b-c"],
)
def test_sync_ids_that_are_safe_in_a_mirror_uid_are_accepted(sync_id):
    assert load_config(BARE.replace("id: only", f"id: {sync_id}"), env=ENV).syncs[0].id == sync_id


@pytest.mark.parametrize(
    "sync_id",
    [
        '"a b"',  # a space would not survive as a UID component
        '"a@b"',
        '"a.b"',
        '"a:b"',
        '"-leading"',  # a leading hyphen makes the UID split ambiguous
        '"_leading"',
        '""',
    ],
)
def test_sync_ids_that_would_corrupt_a_mirror_uid_are_rejected(sync_id):
    # The sync id is embedded in every mirror's UID, which is the identity channel
    # that survives a server stripping custom properties. A id that cannot be parsed
    # back out of a UID would make every mirror invisible to its own sync.
    text = BARE.replace("id: only", f"id: {sync_id}")
    with pytest.raises(ConfigError, match="id"):
        load_config(text, env=ENV)


def test_a_rejected_sync_id_is_not_echoed_back():
    """Interpolation runs before validation, so an id could hold a secret."""
    text = BARE.replace("id: only", "id: ${SECRET_ID}")
    env = dict(ENV, SECRET_ID="fake-not-a-real-secret@example")
    with pytest.raises(ConfigError) as excinfo:
        load_config(text, env=env)
    assert "fake-not-a-real-secret" not in str(excinfo.value)


def test_an_ics_feed_is_accepted_as_a_source():
    config = load_config(ICS, env=ENV)
    account = config.accounts["trips"]
    assert account.type == "ics"
    assert account.url.endswith("/tripit.ics")


def test_an_ics_feed_as_a_dest_is_rejected():
    """A feed is a static document; writing to it is impossible, not merely unwired.

    Caught at load time the user is told once; caught at write time every pass
    would fail after the source had already been read.
    """
    text = ICS.replace("source: {account: trips", "source: {account: home").replace(
        "dest: {account: home, calendar: Mirror}", "dest: {account: trips, calendar: ignored}"
    )
    with pytest.raises(ConfigError, match="read-only") as excinfo:
        load_config(text, env=ENV)
    assert "trips-to-home" in str(excinfo.value)


def test_the_calendar_named_on_an_ics_reference_is_accepted_whatever_it_says():
    """A feed is one calendar and has no list to pick from, so the name is ignored."""
    config = load_config(ICS.replace("calendar: ignored", "calendar: anything at all"), env=ENV)
    assert config.syncs[0].source.calendar == "anything at all"


def test_the_url_of_an_ics_feed_is_never_echoed_by_a_validation_error():
    """The feed URL is a credential: TripIt embeds a token in the path."""
    fake_token = "fake-feed-token-not-a-real-secret"
    text = ICS.replace("type: ics", "type: ics\n    oops: 1").replace(
        "not-a-real-token", "${FEED_TOKEN}"
    )
    with pytest.raises(ConfigError) as excinfo:
        load_config(text, env=dict(ENV, FEED_TOKEN=fake_token))
    assert fake_token not in str(excinfo.value)


def test_merge_deep_merges_nested_blocks():
    defaults = {"padding": {"before": "15m", "after": "15m"}}
    merged = _merge(defaults, {"padding": {"before": "0m"}})
    assert merged["padding"] == {"before": "0m", "after": "15m"}


def test_merge_prefers_the_override():
    merged = _merge({"privacy": "busy", "title": "Busy"}, {"privacy": "full"})
    assert merged == {"privacy": "full", "title": "Busy"}


def test_merge_does_not_share_nested_blocks_with_its_inputs():
    defaults = {"padding": {"before": "15m", "after": "15m"}}
    merged = _merge(defaults, {"id": "a"})
    merged["padding"]["before"] = "99m"
    assert defaults["padding"]["before"] == "15m"


def test_load_config_file_round_trips(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(MINIMAL)
    config = load_config_file(path, env=ENV)
    assert config.accounts["home"].username == "me@example.com"
    assert [sync.id for sync in config.syncs] == ["personal-to-work", "work-to-shared"]


def test_load_config_file_reports_a_missing_file(tmp_path):
    with pytest.raises(ConfigError, match="cannot read config"):
        load_config_file(tmp_path / "nope.yaml", env=ENV)
