import os
import time
from datetime import UTC, datetime, timedelta

from calsync import cli
from calsync.cli import main
from calsync.model import CalEvent
from calsync.providers.fake import FakeProvider

CONFIG = """
accounts:
  a: {type: google, client_id: c, client_secret: s, refresh_token: r}
  b: {type: google, client_id: c, client_secret: s, refresh_token: r}
syncs:
  - id: s1
    source: {account: a, calendar: primary}
    dest: {account: b, calendar: primary}
"""

# Secrets arrive by interpolation, exactly as they do in the container. The
# account is missing ``client_id``, so the validation error's location sits
# directly beside two secrets in the same mapping.
SECRET_NEXT_TO_ERROR = """
accounts:
  a:
    type: google
    client_secret: "${GOOGLE_CLIENT_SECRET}"
    refresh_token: "${GOOGLE_REFRESH_TOKEN}"
  b: {type: google, client_id: c, client_secret: s, refresh_token: r}
syncs:
  - id: s1
    source: {account: a, calendar: primary}
    dest: {account: b, calendar: primary}
"""

SECRETS_IN_ACCOUNTS = """
accounts:
  a:
    type: google
    client_id: c
    client_secret: "${GOOGLE_CLIENT_SECRET}"
    refresh_token: "${GOOGLE_REFRESH_TOKEN}"
  b: {type: google, client_id: c, client_secret: s, refresh_token: r}
syncs:
  - id: s1
    source: {account: a, calendar: primary}
    dest: {account: b, calendar: primary}
"""

CLIENT_SECRET = "client-secret-must-not-be-printed"
REFRESH_TOKEN = "refresh-token-must-not-be-printed"


def write_config(tmp_path, text: str = CONFIG):
    path = tmp_path / "config.yaml"
    path.write_text(text)
    return str(path)


def event(uid: str) -> CalEvent:
    start = datetime.now(UTC).replace(microsecond=0)
    return CalEvent(uid=uid, start=start, end=start + timedelta(hours=1), title="Standup")


def test_validate_accepts_a_good_config(tmp_path, capsys):
    assert main(["validate", "--config", write_config(tmp_path)]) == 0
    assert "s1" in capsys.readouterr().out


def test_validate_rejects_a_bad_config(tmp_path, capsys):
    path = write_config(tmp_path, CONFIG.replace("account: a", "account: missing"))
    assert main(["validate", "--config", path]) == 1
    assert "unknown account" in capsys.readouterr().err


def test_sync_once_dry_run_writes_nothing(tmp_path, capsys):
    dest = FakeProvider()
    providers = {"a": FakeProvider([event("x")]), "b": dest}

    code = main(
        ["sync", "--once", "--dry-run", "--config", write_config(tmp_path)],
        factory=lambda name, account, calendar: providers[name],
    )

    assert code == 0
    assert dest.calls == []
    assert dest.events == {}
    assert "would create=1" in capsys.readouterr().out


def test_sync_once_applies_and_reports(tmp_path, capsys):
    dest = FakeProvider()
    providers = {"a": FakeProvider([event("x")]), "b": dest}

    code = main(
        ["sync", "--once", "--config", write_config(tmp_path)],
        factory=lambda name, account, calendar: providers[name],
    )

    assert code == 0
    assert len(dest.events) == 1
    assert "create=1" in capsys.readouterr().out


def test_sync_once_exits_nonzero_when_a_sync_fails(tmp_path):
    class Exploding(FakeProvider):
        def list_events(self, window):
            raise RuntimeError("boom")

    providers = {"a": Exploding(), "b": FakeProvider()}
    code = main(
        ["sync", "--once", "--config", write_config(tmp_path)],
        factory=lambda name, account, calendar: providers[name],
    )
    assert code == 1


def test_healthcheck_passes_on_a_fresh_heartbeat(tmp_path, monkeypatch):
    heartbeat = tmp_path / "heartbeat"
    heartbeat.touch()
    monkeypatch.setenv("SYNC_INTERVAL", "15m")
    assert main(["healthcheck", "--heartbeat", str(heartbeat)]) == 0


def test_healthcheck_fails_on_a_stale_or_missing_heartbeat(tmp_path, monkeypatch):
    heartbeat = tmp_path / "heartbeat"
    monkeypatch.setenv("SYNC_INTERVAL", "1m")
    assert main(["healthcheck", "--heartbeat", str(heartbeat)]) == 1

    heartbeat.touch()
    stale = time.time() - 600
    os.utime(heartbeat, (stale, stale))
    assert main(["healthcheck", "--heartbeat", str(heartbeat)]) == 1


def test_the_sync_loop_writes_the_heartbeat_the_flag_points_at(tmp_path, monkeypatch):
    """The healthcheck reads ``--heartbeat``; the loop has to be able to write there.

    ``run_forever`` is unbounded in production, so the test bounds it -- but it
    is the real loop, and the assertion is the file the real ``record_heartbeat``
    actually created.
    """
    heartbeat = tmp_path / "elsewhere" / "heartbeat"
    heartbeat.parent.mkdir()
    providers = {"a": FakeProvider(), "b": FakeProvider()}
    unbounded = cli.run_forever

    def bounded(*args, **kwargs):
        unbounded(*args, **kwargs, passes=1, sleep=lambda _: None)

    monkeypatch.setattr(cli, "run_forever", bounded)

    code = main(
        ["sync", "--config", write_config(tmp_path), "--heartbeat", str(heartbeat)],
        factory=lambda name, account, calendar: providers[name],
    )

    assert code == 0
    assert heartbeat.exists()


def test_a_config_error_beside_a_secret_does_not_print_the_secret(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", CLIENT_SECRET)
    monkeypatch.setenv("GOOGLE_REFRESH_TOKEN", REFRESH_TOKEN)
    path = write_config(tmp_path, SECRET_NEXT_TO_ERROR)

    assert main(["validate", "--config", path]) == 1

    captured = capsys.readouterr()
    assert "client_id" in captured.err
    assert CLIENT_SECRET not in captured.out + captured.err
    assert REFRESH_TOKEN not in captured.out + captured.err


def test_a_sync_pass_never_prints_a_secret(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", CLIENT_SECRET)
    monkeypatch.setenv("GOOGLE_REFRESH_TOKEN", REFRESH_TOKEN)
    providers = {"a": FakeProvider([event("x")]), "b": FakeProvider()}

    code = main(
        ["sync", "--once", "--config", write_config(tmp_path, SECRETS_IN_ACCOUNTS)],
        factory=lambda name, account, calendar: providers[name],
    )

    captured = capsys.readouterr()
    assert code == 0
    assert CLIENT_SECRET not in captured.out + captured.err
    assert REFRESH_TOKEN not in captured.out + captured.err


def test_validate_never_prints_a_secret(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", CLIENT_SECRET)
    monkeypatch.setenv("GOOGLE_REFRESH_TOKEN", REFRESH_TOKEN)

    assert main(["validate", "--config", write_config(tmp_path, SECRETS_IN_ACCOUNTS)]) == 0

    captured = capsys.readouterr()
    assert CLIENT_SECRET not in captured.out + captured.err
    assert REFRESH_TOKEN not in captured.out + captured.err


def test_auth_prints_the_token_and_names_the_variable_to_paste_it_into(capsys, monkeypatch):
    monkeypatch.setattr(cli, "mint_refresh_token", lambda *args, **kwargs: "1//minted")

    assert main(["auth", "--client-id", "c", "--client-secret", "s"]) == 0

    out = capsys.readouterr().out
    assert "1//minted" in out
    assert "GOOGLE_REFRESH_TOKEN=1//minted" in out


def test_auth_names_the_variable_the_operator_asked_for(capsys, monkeypatch):
    monkeypatch.setattr(cli, "mint_refresh_token", lambda *args, **kwargs: "1//minted")

    code = main(
        ["auth", "--client-id", "c", "--client-secret", "s", "--env-var", "GOOGLE_TOKEN_WORK"]
    )

    assert code == 0
    assert "GOOGLE_TOKEN_WORK=1//minted" in capsys.readouterr().out


def test_auth_binds_every_interface_by_default(monkeypatch):
    """The container path is the documented one, so it works without a flag."""
    seen: dict = {}

    def fake_mint(*args, **kwargs):
        seen.update(kwargs)
        return "1//minted"

    monkeypatch.setattr(cli, "mint_refresh_token", fake_mint)

    assert main(["auth", "--client-id", "c", "--client-secret", "s"]) == 0
    assert seen["bind_addr"] == "0.0.0.0"


def test_auth_bind_flag_narrows_the_listening_interface(monkeypatch):
    """Run outside a container and you can keep the server off the network."""
    seen: dict = {}

    def fake_mint(*args, **kwargs):
        seen.update(kwargs)
        return "1//minted"

    monkeypatch.setattr(cli, "mint_refresh_token", fake_mint)

    code = main(["auth", "--client-id", "c", "--client-secret", "s", "--bind", "127.0.0.1"])

    assert code == 0
    assert seen["bind_addr"] == "127.0.0.1"


def test_auth_needs_client_credentials(capsys, monkeypatch):
    monkeypatch.delenv("GOOGLE_CLIENT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_CLIENT_SECRET", raising=False)

    assert main(["auth"]) == 1
    assert "--client-id" in capsys.readouterr().err
