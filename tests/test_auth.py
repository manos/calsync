import logging

import pytest

from calsync.auth import client_config, mint_refresh_token


class StubFlow:
    def __init__(self, refresh_token: str | None):
        self._refresh_token = refresh_token
        self.kwargs: dict = {}

    def run_local_server(self, **kwargs):
        self.kwargs = kwargs

    @property
    def credentials(self):
        class Creds:
            refresh_token = self._refresh_token

        return Creds()


def test_client_config_has_the_installed_app_shape():
    config = client_config("cid", "csec")
    installed = config["installed"]
    assert installed["client_id"] == "cid"
    assert installed["client_secret"] == "csec"
    assert installed["token_uri"] == "https://oauth2.googleapis.com/token"
    assert "http://localhost" in installed["redirect_uris"]


def test_mint_returns_the_refresh_token_and_forces_consent():
    flow = StubFlow("rtok")
    token = mint_refresh_token("cid", "csec", port=8080, flow_factory=lambda *_: flow)
    assert token == "rtok"
    assert flow.kwargs["port"] == 8080
    assert flow.kwargs["access_type"] == "offline"
    assert flow.kwargs["prompt"] == "consent"
    assert flow.kwargs["open_browser"] is False


def test_mint_listens_on_every_interface_by_default():
    """The documented path is a container with a published port.

    Docker forwards a published port to the container's bridge address, so a
    server on the container's loopback is unreachable and the browser gets an
    empty reply. Binding every interface is what makes ``-p 8080:8080`` work.
    """
    flow = StubFlow("rtok")
    mint_refresh_token("cid", "csec", flow_factory=lambda *_: flow)
    assert flow.kwargs["bind_addr"] == "0.0.0.0"


def test_mint_binds_where_it_is_told():
    flow = StubFlow("rtok")
    mint_refresh_token("cid", "csec", bind_addr="127.0.0.1", flow_factory=lambda *_: flow)
    assert flow.kwargs["bind_addr"] == "127.0.0.1"


def test_mint_leaves_the_redirect_host_on_localhost():
    """Only the listening interface moves; the browser still hits localhost.

    ``run_local_server`` builds the redirect URI from ``host`` and binds
    ``bind_addr``, and Google only accepts a loopback redirect.
    """
    flow = StubFlow("rtok")
    mint_refresh_token("cid", "csec", bind_addr="0.0.0.0", flow_factory=lambda *_: flow)
    assert flow.kwargs.get("host", "localhost") == "localhost"


def test_missing_refresh_token_is_an_error():
    with pytest.raises(RuntimeError, match="no refresh token"):
        mint_refresh_token("cid", "csec", flow_factory=lambda *_: StubFlow(None))


def test_the_missing_token_error_quotes_no_secret():
    with pytest.raises(RuntimeError) as info:
        mint_refresh_token("cid", "csec", flow_factory=lambda *_: StubFlow(None))
    assert "csec" not in str(info.value)


def test_minting_writes_neither_the_secret_nor_the_token_anywhere(capsys, caplog):
    """The minted token reaches the operator through the return value only."""
    with caplog.at_level(logging.DEBUG):
        mint_refresh_token("cid", "csec", flow_factory=lambda *_: StubFlow("rtok"))

    captured = capsys.readouterr()
    written = captured.out + captured.err + caplog.text
    assert "csec" not in written
    assert "rtok" not in written
