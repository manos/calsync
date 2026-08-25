import pytest

from calsync.config import CaldavAccount, GoogleAccount, IcsAccount
from calsync.providers.base import ProviderError
from calsync.providers.caldav import CaldavProvider
from calsync.providers.factory import build_provider, caldav_calendar, google_credentials
from calsync.providers.ics import IcsProvider

GOOGLE = GoogleAccount(type="google", client_id="cid", client_secret="csec", refresh_token="rtok")
ICLOUD = CaldavAccount(
    type="caldav", url="https://caldav.icloud.com", username="me@example.com", password="pw"
)
FEED_TOKEN = "fake-feed-token-not-a-real-secret"
FEED = IcsAccount(type="ics", url=f"https://www.tripit.com/feed/ical/private/{FEED_TOKEN}/x.ics")


class StubClient:
    """The DAVClient the factory opens; it owns the pooled HTTP session."""

    def __init__(self) -> None:
        self.closed = 0

    def close(self) -> None:
        self.closed += 1


class StubCalendarHandle:
    def __init__(self, name: str, client: StubClient | None = None):
        self._name = name
        # caldav hangs the discovering DAVClient off every object it returns.
        self.client = client

    def get_display_name(self) -> str:
        return self._name


class StubPrincipal:
    def __init__(self, names: list[str], client: StubClient | None = None):
        self._calendars = [StubCalendarHandle(name, client) for name in names]

    def get_calendars(self):
        return self._calendars


def test_google_credentials_carry_the_account_fields():
    creds = google_credentials(GOOGLE)
    assert creds.refresh_token == "rtok"
    assert creds.client_id == "cid"
    assert creds.client_secret == "csec"
    assert "https://www.googleapis.com/auth/calendar" in creds.scopes


def test_caldav_calendar_is_matched_by_display_name_case_insensitively():
    handle = caldav_calendar(StubPrincipal(["Home", "Work"]), "home")
    assert handle.get_display_name() == "Home"


def test_unknown_caldav_calendar_lists_what_is_available():
    with pytest.raises(ProviderError, match="Home, Work"):
        caldav_calendar(StubPrincipal(["Home", "Work"]), "Missing")


def test_build_provider_dispatches_on_account_type(monkeypatch):
    monkeypatch.setattr(
        "calsync.providers.factory.caldav_principal", lambda account: StubPrincipal(["Home"])
    )
    provider = build_provider("icloud_personal", ICLOUD, "Home")
    assert isinstance(provider, CaldavProvider)
    assert provider.name == "icloud_personal/Home"


def test_build_provider_surfaces_the_available_calendar_names(monkeypatch):
    monkeypatch.setattr(
        "calsync.providers.factory.caldav_principal",
        lambda account: StubPrincipal(["Home", "Work"]),
    )
    with pytest.raises(ProviderError, match="Home, Work"):
        build_provider("icloud_personal", ICLOUD, "Missing")


def test_build_provider_builds_a_reader_for_an_ics_feed():
    """No credential exchange to do: the feed is fetched fresh on every pass."""
    provider = build_provider("trips", FEED, "ignored")
    assert isinstance(provider, IcsProvider)
    assert provider.name == "trips/ignored"


def test_build_provider_never_leaks_the_feed_url(monkeypatch):
    """The feed URL is the credential; a chatty failure must not carry it out."""

    def explode(*args, **kwargs):
        raise RuntimeError(f"refused: {FEED.url}")

    monkeypatch.setattr("calsync.providers.factory.IcsProvider", explode)
    with pytest.raises(ProviderError) as info:
        build_provider("trips", FEED, "ignored")

    message = str(info.value)
    assert FEED_TOKEN not in message
    assert "trips/ignored" in message
    # log.exception() prints the whole chain, so the raw text must not be chained either.
    assert info.value.__cause__ is None
    assert info.value.__suppress_context__ is True


def test_build_provider_never_leaks_google_secrets(monkeypatch):
    """A chatty library failure must not carry the token out of the factory."""

    def explode(*args, **kwargs):
        raise RuntimeError("refused: client_secret=csec refresh_token=rtok")

    monkeypatch.setattr("calsync.providers.factory.build_service", explode)
    with pytest.raises(ProviderError) as info:
        build_provider("work_google", GOOGLE, "primary")

    message = str(info.value)
    assert "csec" not in message
    assert "rtok" not in message
    assert "work_google/primary" in message
    assert "RuntimeError" in message
    # log.exception() prints the whole chain, so the raw text must not be chained either.
    assert info.value.__cause__ is None
    assert info.value.__suppress_context__ is True


def test_build_provider_never_leaks_the_caldav_password(monkeypatch):
    account = CaldavAccount(
        type="caldav",
        url="https://caldav.icloud.com",
        username="me@example.com",
        password="app-specific-abcd",
    )

    def explode(_account):
        raise RuntimeError("401 for https://me%40example.com:app-specific-abcd@caldav.icloud.com")

    monkeypatch.setattr("calsync.providers.factory.caldav_principal", explode)
    with pytest.raises(ProviderError) as info:
        build_provider("icloud_personal", account, "Home")

    message = str(info.value)
    assert "app-specific-abcd" not in message
    assert info.value.__suppress_context__ is True


def test_the_caldav_provider_it_builds_can_release_the_session(monkeypatch):
    """The factory is the only thing that opens a connection, so it owes a way to shut one.

    Nothing here closed the DAVClient it opened, and run_pass rebuilds every
    provider each pass, so a long-running container leaked one session per
    CalDAV calendar per pass.
    """
    client = StubClient()
    monkeypatch.setattr(
        "calsync.providers.factory.caldav_principal",
        lambda account: StubPrincipal(["Home"], client),
    )

    build_provider("icloud_personal", ICLOUD, "Home").close()

    assert client.closed == 1
