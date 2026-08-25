"""Command line entry point."""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from calsync.auth import mint_refresh_token
from calsync.config import ConfigError, load_config_file, parse_duration
from calsync.providers.factory import build_provider
from calsync.runner import DEFAULT_HEARTBEAT, ProviderFactory, run_forever, run_pass

DEFAULT_CONFIG_PATH = "/config.yaml"
DEFAULT_TOKEN_VAR = "GOOGLE_REFRESH_TOKEN"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="calsync", description="Mirror calendars, statelessly.")
    subcommands = parser.add_subparsers(dest="command", required=True)

    sync = subcommands.add_parser("sync", help="run syncs on an interval, or once")
    sync.add_argument("--config", default=os.environ.get("CALSYNC_CONFIG", DEFAULT_CONFIG_PATH))
    sync.add_argument("--once", action="store_true", help="run a single pass and exit")
    sync.add_argument("--dry-run", action="store_true", help="report operations, change nothing")
    # The healthcheck reads this file; the loop writes it. They have to be able
    # to point at the same path, or a container with a relocated heartbeat is
    # permanently unhealthy while syncing perfectly.
    sync.add_argument(
        "--heartbeat",
        default=str(DEFAULT_HEARTBEAT),
        help="file to touch after a wholly successful pass",
    )

    validate = subcommands.add_parser("validate", help="check the config and exit")
    validate.add_argument("--config", default=os.environ.get("CALSYNC_CONFIG", DEFAULT_CONFIG_PATH))

    auth = subcommands.add_parser("auth", help="mint a Google refresh token")
    auth.add_argument("--client-id", default=os.environ.get("GOOGLE_CLIENT_ID"))
    auth.add_argument("--client-secret", default=os.environ.get("GOOGLE_CLIENT_SECRET"))
    auth.add_argument("--port", type=int, default=8080)
    # Every interface by default, because the documented way to run this is a
    # container with a published port and Docker forwards that to the bridge
    # address, not the container's loopback. Running from a checkout instead,
    # --bind 127.0.0.1 keeps the server off the network.
    auth.add_argument(
        "--bind",
        default="0.0.0.0",
        help="interface the one-shot OAuth server listens on (default: all)",
    )
    # One variable per Google account, so the operator names the one this token
    # belongs to and the printed line is paste-ready as it stands.
    auth.add_argument(
        "--env-var",
        default=DEFAULT_TOKEN_VAR,
        help="name of the environment variable this token will be read from",
    )

    health = subcommands.add_parser("healthcheck", help="check the sync heartbeat")
    health.add_argument("--heartbeat", default=str(DEFAULT_HEARTBEAT))

    return parser


def _configure_logging() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "info").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
    )


def main(argv: list[str] | None = None, factory: ProviderFactory = build_provider) -> int:
    args = _parser().parse_args(argv)
    _configure_logging()

    if args.command == "auth":
        if not args.client_id or not args.client_secret:
            print("--client-id and --client-secret are required", file=sys.stderr)
            return 1
        token = mint_refresh_token(
            args.client_id, args.client_secret, port=args.port, bind_addr=args.bind
        )
        # The only place calsync prints a secret, and deliberately: this is the
        # one moment the operator can capture it.
        print("\nrefresh token minted. Put it in calsync's environment as:\n")
        print(f"{args.env_var}={token}\n")
        return 0

    if args.command == "healthcheck":
        interval = parse_duration(os.environ.get("SYNC_INTERVAL", "15m"))
        path = Path(args.heartbeat)
        if not path.exists():
            return 1
        age = time.time() - path.stat().st_mtime
        return 0 if age < 2 * interval.total_seconds() else 1

    try:
        config = load_config_file(args.config, env=os.environ)
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if args.command == "validate":
        for spec in config.syncs:
            print(
                f"{spec.id}: {spec.source.account}/{spec.source.calendar} -> "
                f"{spec.dest.account}/{spec.dest.calendar} "
                f"privacy={spec.privacy} padding=-{spec.padding.before}/+{spec.padding.after}"
            )
        return 0

    if args.once or args.dry_run:
        results = run_pass(config, factory, now=datetime.now(UTC), dry_run=args.dry_run)
        for result in results:
            print(result.summary())
        return 0 if all(result.ok for result in results) else 1

    interval = parse_duration(os.environ.get("SYNC_INTERVAL", "15m"))
    run_forever(config, factory, interval=interval, heartbeat=Path(args.heartbeat))
    return 0
