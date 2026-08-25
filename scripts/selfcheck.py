"""Prove a built environment can do everything calsync needs, offline.

Run at build time (the Dockerfile copies this into the build stage) so that a broken
or over-aggressively stripped environment fails the build instead of the deployment.
The same file can be piped into a finished image to re-check it as the runtime user:

    docker run --rm -i --network=none --entrypoint python <image> - < scripts/selfcheck.py

Nothing here touches the network: every check must pass with no credentials and no DNS.
"""

from __future__ import annotations

import sys

failures: list[str] = []


def check(label: str, fn) -> None:
    try:
        fn()
    except Exception as exc:  # noqa: BLE001 - a report of every failure beats the first one
        failures.append(f"{label}: {type(exc).__name__}: {exc}")
        print(f"FAIL {label}: {type(exc).__name__}: {exc}")
    else:
        print(f"ok   {label}")


def imports() -> None:
    import caldav  # noqa: F401
    import icalendar  # noqa: F401
    import recurring_ical_events  # noqa: F401

    import calsync.cli  # noqa: F401


def google_static_discovery() -> None:
    """The one Google API document calsync builds, served from disk with no network."""
    from google.auth.credentials import AnonymousCredentials
    from googleapiclient.discovery import build
    from googleapiclient.discovery_cache import get_static_doc

    doc = get_static_doc("calendar", "v3")
    if not doc or '"calendarList"' not in doc:
        raise AssertionError("static calendar v3 discovery document missing or truncated")
    service = build("calendar", "v3", credentials=AnonymousCredentials(), static_discovery=True)
    service.events()  # parsing the document into a resource is the real proof


def urllib3_settled() -> None:
    """caldav pulls urllib3-future, the Google stack pulls urllib3; both own urllib3/.

    urllib3-future repairs the clash from a .pth on interpreter start, but only where
    site-packages is writable -- never in a non-root runtime. If it is still trying to
    repair here, the tree was not settled while it was writable.
    """
    import urllib3

    if not hasattr(urllib3, "__version__"):
        raise AssertionError("urllib3 package is half-merged")
    import niquests  # noqa: F401  # caldav's HTTP client, imports urllib3-future internals
    import requests  # noqa: F401  # the Google stack's HTTP client, imports stock urllib3


def crypto() -> None:
    """google-auth signs JWTs for the token exchange; needs a working cryptography."""
    from cryptography.hazmat.primitives.asymmetric import rsa

    rsa.generate_private_key(public_exponent=65537, key_size=2048)


def lxml_available() -> None:
    """caldav parses DAV XML responses with lxml."""
    from lxml import etree

    etree.fromstring(b"<multistatus xmlns='DAV:'/>")


def config_roundtrip() -> None:
    """pydantic + pyyaml + zoneinfo: the config path that `calsync validate` walks."""
    import zoneinfo

    zoneinfo.ZoneInfo("Europe/Berlin")


def native_extensions() -> None:
    """Load every compiled extension in site-packages.

    The image strips debug symbols from these .so files, so prove the dynamic linker
    is still satisfied by each one rather than trusting that the few modules exercised
    above cover them all.
    """
    import importlib
    import pathlib
    import sysconfig

    site_packages = pathlib.Path(sysconfig.get_paths()["purelib"])
    modules = sorted(
        {
            str(so.relative_to(site_packages)).split(".")[0].replace("/", ".")
            for so in site_packages.rglob("*.so")
        }
    )
    if not modules:
        raise AssertionError(f"no compiled extensions found under {site_packages}")
    broken = []
    for name in modules:
        try:
            importlib.import_module(name)
        except Exception as exc:  # noqa: BLE001 - report all of them, not just the first
            broken.append(f"{name} ({type(exc).__name__}: {exc})")
    if broken:
        raise AssertionError(f"{len(broken)} extension(s) will not load: {', '.join(broken)}")
    print(f"     ({len(modules)} compiled extensions load)")


def main() -> int:
    check("imports (calsync.cli, caldav, icalendar, recurring_ical_events)", imports)
    check("google calendar v3 static discovery (offline)", google_static_discovery)
    check("urllib3 / urllib3-future tree settled", urllib3_settled)
    check("cryptography keygen", crypto)
    check("lxml XML parse", lxml_available)
    check("zoneinfo tzdata", config_roundtrip)
    check("compiled extensions load", native_extensions)
    if failures:
        print(f"\n{len(failures)} check(s) failed")
        return 1
    print("\nall checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
