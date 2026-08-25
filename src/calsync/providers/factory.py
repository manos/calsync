"""Build live providers from account configuration. The only module that connects."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import caldav
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build as build_service

from calsync.config import Account, CaldavAccount, GoogleAccount, IcsAccount
from calsync.providers.base import Provider, ProviderError
from calsync.providers.caldav import CaldavProvider
from calsync.providers.google import GoogleProvider
from calsync.providers.ics import IcsProvider

GOOGLE_SCOPES = ["https://www.googleapis.com/auth/calendar"]
GOOGLE_TOKEN_URI = "https://oauth2.googleapis.com/token"


def _redact(exc: Exception, secrets: Iterable[str]) -> str:
    """Describe a connection failure without repeating any secret it quoted.

    Everything here is handed credentials, and a library is free to put them
    into its own error text -- caldav formats the request URL, which carries the
    password when one is embedded in it. The type name and the redacted message
    keep the useful part ('invalid_grant', '401 Unauthorized') readable.
    """
    text = f"{type(exc).__name__}: {exc}"
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    return text


def google_credentials(account: GoogleAccount) -> Credentials:
    return Credentials(
        token=None,
        refresh_token=account.refresh_token,
        client_id=account.client_id,
        client_secret=account.client_secret,
        token_uri=GOOGLE_TOKEN_URI,
        scopes=GOOGLE_SCOPES,
    )


def caldav_principal(account: CaldavAccount) -> Any:
    client = caldav.DAVClient(url=account.url, username=account.username, password=account.password)
    return client.get_principal()


def caldav_calendar(principal: Any, name: str) -> Any:
    available = principal.get_calendars()
    for handle in available:
        if str(handle.get_display_name()).lower() == name.lower():
            return handle
    names = ", ".join(sorted(str(handle.get_display_name()) for handle in available))
    raise ProviderError(f"calendar '{name}' not found; available calendars: {names}")


def build_provider(account_name: str, account: Account, calendar: str) -> Provider:
    label = f"{account_name}/{calendar}"
    if isinstance(account, GoogleAccount):
        try:
            service = build_service(
                "calendar", "v3", credentials=google_credentials(account), cache_discovery=False
            )
        except Exception as exc:
            secrets = (account.client_secret, account.refresh_token)
            # ``from None`` so log.exception() cannot print the unredacted original.
            raise ProviderError(
                f"cannot reach Google for '{label}': {_redact(exc, secrets)}"
            ) from None
        return GoogleProvider(service, calendar_id=calendar, name=label)
    if isinstance(account, CaldavAccount):
        try:
            handle = caldav_calendar(caldav_principal(account), calendar)
        except ProviderError:
            # Ours already, and quotes no secret -- the available-calendar list is
            # the most useful message in the product, so it passes through intact.
            raise
        except Exception as exc:
            raise ProviderError(
                f"cannot reach CalDAV for '{label}': {_redact(exc, (account.password,))}"
            ) from None
        return CaldavProvider(handle, email=account.username, name=label)
    if isinstance(account, IcsAccount):
        # Nothing is connected here: a feed is fetched afresh on every pass, so the
        # only way this fails is a URL that will not even parse. The whole URL is a
        # credential, so that failure is redacted like any other.
        try:
            return IcsProvider(account.url, name=label)
        except Exception as exc:
            raise ProviderError(
                f"cannot use the ICS feed for '{label}': {_redact(exc, (account.url,))}"
            ) from None
    raise ProviderError(f"unsupported account type for '{account_name}'")
