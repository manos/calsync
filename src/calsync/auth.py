"""One-shot OAuth loopback flow for minting Google refresh tokens.

Personal Gmail accounts are in scope, so a service account with domain-wide
delegation is not an option: the refresh token has to be minted by the human
who owns the calendar. Nothing here writes the client secret or the token
anywhere -- the token leaves by the return value, and it is the caller's job to
decide whether a human is looking at the terminal.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from google_auth_oauthlib.flow import InstalledAppFlow

from calsync.providers.factory import GOOGLE_SCOPES, GOOGLE_TOKEN_URI


def client_config(client_id: str, client_secret: str) -> dict:
    return {
        "installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": GOOGLE_TOKEN_URI,
            "redirect_uris": ["http://localhost"],
        }
    }


def _default_flow(client_id: str, client_secret: str) -> Any:
    return InstalledAppFlow.from_client_config(
        client_config(client_id, client_secret), scopes=GOOGLE_SCOPES
    )


def mint_refresh_token(
    client_id: str,
    client_secret: str,
    port: int = 8080,
    bind_addr: str = "0.0.0.0",
    flow_factory: Callable[[str, str], Any] = _default_flow,
) -> str:
    """Run the loopback flow and return a long-lived refresh token.

    ``flow_factory`` is injected so the test never opens a socket.
    ``open_browser=False`` because this runs in a container: the library prints
    the consent URL for the operator to paste into a browser on the host.
    ``access_type=offline`` with ``prompt=consent`` is what makes Google issue a
    refresh token at all, and re-issue one on a repeat authorisation.

    ``bind_addr`` defaults to every interface because the documented way to run
    this is a container with a published port, and Docker forwards a published
    port to the container's bridge address -- a server on the container's
    loopback is unreachable from there, and the browser gets an empty reply.
    The redirect URI stays ``http://localhost:<port>``, which is what the
    browser hits and the only shape Google accepts: ``run_local_server`` builds
    that from ``host`` and binds ``bind_addr`` separately. Run outside a
    container and ``bind_addr="127.0.0.1"`` keeps the server off the network
    for the seconds the approval takes.
    """
    flow = flow_factory(client_id, client_secret)
    flow.run_local_server(
        port=port,
        bind_addr=bind_addr,
        open_browser=False,
        access_type="offline",
        prompt="consent",
    )
    token = flow.credentials.refresh_token
    if not token:
        raise RuntimeError(
            "Google returned no refresh token; revoke the app's access at "
            "https://myaccount.google.com/permissions and try again"
        )
    return token
