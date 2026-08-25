import logging
from datetime import UTC, datetime, timedelta

from calsync.config import Config, load_config
from calsync.model import CalEvent
from calsync.providers.fake import FakeProvider
from calsync.runner import run_forever, run_pass

NOW = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)

CONFIG = """
accounts:
  a: {type: google, client_id: c, client_secret: s, refresh_token: r}
  b: {type: google, client_id: c, client_secret: s, refresh_token: r}
syncs:
  - id: s1
    source: {account: a, calendar: primary}
    dest: {account: b, calendar: primary}
  - id: s2
    source: {account: b, calendar: primary}
    dest: {account: a, calendar: primary}
"""

# Two calendars on one account: the provider cache is keyed by the pair, not by
# the account alone.
TWO_CALENDARS = """
accounts:
  a: {type: google, client_id: c, client_secret: s, refresh_token: r}
  b: {type: google, client_id: c, client_secret: s, refresh_token: r}
syncs:
  - id: work
    source: {account: a, calendar: work}
    dest: {account: b, calendar: primary}
  - id: personal
    source: {account: a, calendar: personal}
    dest: {account: b, calendar: primary}
"""


def event(uid: str) -> CalEvent:
    return CalEvent(
        uid=uid,
        start=datetime(2026, 5, 2, 9, 0, tzinfo=UTC),
        end=datetime(2026, 5, 2, 10, 0, tzinfo=UTC),
        title="Standup",
    )


def factory_from(providers: dict[tuple[str, str], FakeProvider]):
    def factory(account_name, account, calendar):
        return providers[(account_name, calendar)]

    return factory


def test_a_pass_runs_every_sync():
    providers = {("a", "primary"): FakeProvider([event("x")]), ("b", "primary"): FakeProvider()}
    config = load_config(CONFIG, env={})
    results = run_pass(config, factory_from(providers), now=NOW)

    assert [r.sync_id for r in results] == ["s1", "s2"]
    assert results[0].created == 1


def test_a_dry_run_pass_reports_what_it_would_do_without_writing():
    source = FakeProvider([event("x")])
    dest = FakeProvider()
    providers = {("a", "primary"): source, ("b", "primary"): dest}
    config = load_config(CONFIG, env={})

    results = run_pass(config, factory_from(providers), now=NOW, dry_run=True)

    assert results[0].created == 1
    assert results[0].dry_run is True
    assert dest.events == {}
    assert dest.calls == []
    assert list(source.events) == ["x"]


def test_mirrors_do_not_ping_pong_between_reciprocal_syncs():
    providers = {("a", "primary"): FakeProvider([event("x")]), ("b", "primary"): FakeProvider()}
    config = load_config(CONFIG, env={})

    run_pass(config, factory_from(providers), now=NOW)
    second = run_pass(config, factory_from(providers), now=NOW)

    assert [(r.created, r.updated, r.deleted) for r in second] == [(0, 0, 0), (0, 0, 0)]
    assert len(providers[("a", "primary")].events) == 1
    assert len(providers[("b", "primary")].events) == 1


class Exploding(FakeProvider):
    """Explodes when read as a source; still usable as a destination.

    ``FakeProvider.list_mirrors`` delegates to ``list_events``, so overriding
    only ``list_events`` would break this provider in both roles -- and every
    account here is one sync's source and the other's dest.
    """

    def list_events(self, window):
        raise RuntimeError("boom")

    def list_mirrors(self, window, sync_id):
        return []


def test_one_failing_sync_does_not_stop_the_others():
    providers = {("a", "primary"): Exploding(), ("b", "primary"): FakeProvider([event("y")])}
    config = load_config(CONFIG, env={})
    results = run_pass(config, factory_from(providers), now=NOW)

    assert results[0].ok is False
    assert "boom" in results[0].error
    assert results[1].ok is True
    assert results[1].created == 1


def test_providers_are_built_once_per_account_and_calendar():
    built: list[tuple[str, str]] = []
    providers = {("a", "primary"): FakeProvider(), ("b", "primary"): FakeProvider()}

    def counting_factory(account_name, account, calendar):
        built.append((account_name, calendar))
        return providers[(account_name, calendar)]

    run_pass(load_config(CONFIG, env={}), counting_factory, now=NOW)
    assert sorted(built) == [("a", "primary"), ("b", "primary")]


def test_two_calendars_on_one_account_get_their_own_providers():
    built: list[tuple[str, str]] = []
    providers = {
        ("a", "work"): FakeProvider(),
        ("a", "personal"): FakeProvider(),
        ("b", "primary"): FakeProvider(),
    }

    def counting_factory(account_name, account, calendar):
        built.append((account_name, calendar))
        return providers[(account_name, calendar)]

    run_pass(load_config(TWO_CALENDARS, env={}), counting_factory, now=NOW)

    # One provider per (account, calendar): 'a' is built twice for its two
    # calendars, and 'b/primary' is reused across both syncs.
    assert sorted(built) == [("a", "personal"), ("a", "work"), ("b", "primary")]


def test_loop_touches_the_heartbeat_after_a_successful_pass(tmp_path):
    heartbeat = tmp_path / "heartbeat"
    providers = {("a", "primary"): FakeProvider(), ("b", "primary"): FakeProvider()}
    slept: list[float] = []

    run_forever(
        load_config(CONFIG, env={}),
        factory_from(providers),
        interval=timedelta(minutes=15),
        heartbeat=heartbeat,
        clock=lambda: NOW,
        sleep=slept.append,
        passes=2,
    )

    assert heartbeat.exists()
    # Two passes, one gap between them: sleeping after the final pass would make
    # a bounded run hang for a whole interval after its last useful work.
    assert slept == [900.0]


def test_loop_leaves_the_heartbeat_stale_when_a_sync_fails(tmp_path):
    heartbeat = tmp_path / "heartbeat"
    providers = {("a", "primary"): Exploding(), ("b", "primary"): FakeProvider()}

    run_forever(
        load_config(CONFIG, env={}),
        factory_from(providers),
        interval=timedelta(minutes=15),
        heartbeat=heartbeat,
        clock=lambda: NOW,
        sleep=lambda _: None,
        passes=1,
    )

    assert not heartbeat.exists()


def test_loop_names_the_failing_sync_when_it_withholds_the_heartbeat(tmp_path, caplog):
    heartbeat = tmp_path / "heartbeat"
    providers = {("a", "primary"): Exploding(), ("b", "primary"): FakeProvider()}

    with caplog.at_level(logging.WARNING, logger="calsync.runner"):
        run_forever(
            load_config(CONFIG, env={}),
            factory_from(providers),
            interval=timedelta(minutes=15),
            heartbeat=heartbeat,
            clock=lambda: NOW,
            sleep=lambda _: None,
            passes=1,
        )

    assert not heartbeat.exists()
    messages = [
        record.getMessage() for record in caplog.records if record.levelno >= logging.WARNING
    ]
    assert any("heartbeat" in message and "s1" in message for message in messages)


def test_loop_says_so_when_a_pass_produces_no_results(tmp_path, caplog):
    heartbeat = tmp_path / "heartbeat"
    empty = Config(accounts={}, syncs=[])

    with caplog.at_level(logging.WARNING, logger="calsync.runner"):
        run_forever(
            empty,
            factory_from({}),
            interval=timedelta(minutes=15),
            heartbeat=heartbeat,
            clock=lambda: NOW,
            sleep=lambda _: None,
            passes=1,
        )

    assert not heartbeat.exists()
    messages = [
        record.getMessage() for record in caplog.records if record.levelno >= logging.WARNING
    ]
    assert any("heartbeat" in message and "no results" in message for message in messages)


def test_loop_survives_a_heartbeat_it_cannot_write(tmp_path, caplog):
    # A read-only mount or a missing parent directory must not turn a working
    # sync into a container crash loop.
    heartbeat = tmp_path / "missing" / "heartbeat"
    providers = {("a", "primary"): FakeProvider(), ("b", "primary"): FakeProvider()}

    with caplog.at_level(logging.ERROR, logger="calsync.runner"):
        run_forever(
            load_config(CONFIG, env={}),
            factory_from(providers),
            interval=timedelta(minutes=15),
            heartbeat=heartbeat,
            clock=lambda: NOW,
            sleep=lambda _: None,
            passes=2,
        )

    assert not heartbeat.exists()
    errors = [record.getMessage() for record in caplog.records if record.levelno >= logging.ERROR]
    assert any("heartbeat" in message for message in errors)


class WindowRecording(FakeProvider):
    """Records every window it is asked about."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.windows: list[tuple[datetime, datetime]] = []

    def list_events(self, window):
        self.windows.append(window)
        return super().list_events(window)


def test_the_loop_passes_its_injected_clock_down_to_the_syncs(tmp_path):
    providers = {("a", "primary"): WindowRecording(), ("b", "primary"): WindowRecording()}

    run_forever(
        load_config(CONFIG, env={}),
        factory_from(providers),
        interval=timedelta(minutes=15),
        heartbeat=tmp_path / "heartbeat",
        clock=lambda: NOW,
        sleep=lambda _: None,
        passes=1,
    )

    # Default window: one day back, fourteen days forward -- from the injected
    # clock, not from the wall clock.
    expected = (NOW - timedelta(days=1), NOW + timedelta(days=14))
    windows = [window for provider in providers.values() for window in provider.windows]
    assert windows
    assert all(window == expected for window in windows)


def test_the_first_pass_runs_before_the_first_sleep(tmp_path):
    providers = {("a", "primary"): FakeProvider(), ("b", "primary"): FakeProvider()}
    order: list[str] = []

    def recording_factory(account_name, account, calendar):
        order.append("pass")
        return providers[(account_name, calendar)]

    run_forever(
        load_config(CONFIG, env={}),
        recording_factory,
        interval=timedelta(minutes=15),
        heartbeat=tmp_path / "heartbeat",
        clock=lambda: NOW,
        sleep=lambda _: order.append("sleep"),
        passes=2,
    )

    # Sleeping first would idle the container for a whole interval before its
    # first pass, leaving it unhealthy well past the healthcheck start period.
    assert order[0] == "pass"
    assert order == ["pass", "pass", "sleep", "pass", "pass"]


class Closing(FakeProvider):
    """Records that the runner released it."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.closed = 0

    def close(self) -> None:
        self.closed += 1


def test_a_pass_closes_every_provider_it_built():
    # Providers are rebuilt each pass, so a session the pass does not release
    # is a leak that grows for as long as the container runs.
    providers = {("a", "primary"): Closing([event("x")]), ("b", "primary"): Closing()}

    run_pass(load_config(CONFIG, env={}), factory_from(providers), now=NOW)

    assert [provider.closed for provider in providers.values()] == [1, 1]


def test_a_pass_closes_its_providers_even_when_a_sync_failed():
    class ClosingExploding(Closing, Exploding):
        pass

    providers = {("a", "primary"): ClosingExploding(), ("b", "primary"): Closing([event("y")])}

    results = run_pass(load_config(CONFIG, env={}), factory_from(providers), now=NOW)

    assert results[0].ok is False
    assert [provider.closed for provider in providers.values()] == [1, 1]


def test_a_provider_that_will_not_close_fails_neither_the_pass_nor_the_others():
    """Closing is bookkeeping after the work is done.

    A backend that refuses to hang up must not turn a good pass into a failed
    one -- that would withhold the heartbeat and mark the container unhealthy
    over syncs that all succeeded.
    """

    class Stubborn(Closing):
        def close(self) -> None:
            super().close()
            raise RuntimeError("hang up refused")

    # 'b' has no close() at all, which is the Google provider's case: a pass
    # must tolerate both the raising close and the absent one.
    providers = {("a", "primary"): Stubborn([event("x")]), ("b", "primary"): FakeProvider()}

    results = run_pass(load_config(CONFIG, env={}), factory_from(providers), now=NOW)

    assert all(result.ok for result in results)
    assert providers[("a", "primary")].closed == 1
