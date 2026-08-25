"""Configuration loading: YAML in, validated objects out."""

from __future__ import annotations

import copy
import re
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, ValidationError

DURATION_PATTERN = re.compile(r"^(\d+)\s*([smhd])$")
ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
# The sync id is embedded in every mirror's UID (``calsync-<sync_id>-<key>@calsync``),
# which is the identity channel that survives a server dropping custom properties. An
# id outside this alphabet could not be parsed back out of a UID, and a leading hyphen
# or underscore would make the split against the key ambiguous -- either way every
# mirror would go invisible to its own sync and be re-created on every pass.
SYNC_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
_UNITS = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}


class ConfigError(Exception):
    """Raised for any malformed or inconsistent configuration."""


def parse_duration(value: Any) -> timedelta:
    """Accept ``'15m'``, ``'2h'``, ``'14d'``, ``'30s'``, or a raw seconds number."""
    if isinstance(value, timedelta):
        parsed = value
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        parsed = timedelta(seconds=float(value))
    else:
        match = DURATION_PATTERN.match(str(value).strip())
        if not match:
            raise ValueError(f"invalid duration {value!r}; use forms like '15m', '2h', '14d'")
        parsed = timedelta(**{_UNITS[match.group(2)]: int(match.group(1))})
    # A negative window inverts start/end and negative padding shrinks events.
    if parsed < timedelta(0):
        raise ValueError(f"invalid duration {value!r}; durations must not be negative")
    return parsed


Duration = Annotated[timedelta, BeforeValidator(parse_duration)]


def validate_sync_id(value: Any) -> str:
    """Reject any sync id that could not be recovered from a mirror's UID."""
    # The id itself is never echoed: interpolation runs before validation, so it
    # could hold whatever a ``${VAR}`` expanded to.
    if not isinstance(value, str) or not SYNC_ID_PATTERN.match(value):
        raise ValueError(
            "sync id must start with a letter or digit and use only letters, digits, "
            "'_' and '-'; it is embedded in every mirror's UID"
        )
    return value


SyncId = Annotated[str, BeforeValidator(validate_sync_id)]


class Base(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WindowSpec(Base):
    past: Duration = timedelta(days=1)
    future: Duration = timedelta(days=14)

    def bounds(self, now: datetime) -> tuple[datetime, datetime]:
        now = now.astimezone(UTC)
        return now - self.past, now + self.future


class PaddingSpec(Base):
    before: Duration = timedelta(0)
    after: Duration = timedelta(0)


class SkipSpec(Base):
    declined: bool = True
    free: bool = True
    all_day: bool = True
    cancelled: bool = True
    mirrors: bool = True


class CalendarRef(Base):
    """One calendar of one account.

    ``calendar`` is ignored when ``account`` is an ``ics`` feed: a feed is a
    single static document with no calendar list to choose from, so any value is
    accepted there and none of them selects anything.
    """

    account: str
    calendar: str


class SyncSpec(Base):
    id: SyncId
    source: CalendarRef
    dest: CalendarRef
    window: WindowSpec = WindowSpec()
    padding: PaddingSpec = PaddingSpec()
    privacy: Literal["busy", "full"] = "busy"
    title: str = "Busy"
    skip: SkipSpec = SkipSpec()
    max_deletes_per_pass: int = Field(default=20, ge=0)


class GoogleAccount(Base):
    type: Literal["google"]
    client_id: str
    client_secret: str
    refresh_token: str


class CaldavAccount(Base):
    type: Literal["caldav"]
    url: str
    username: str
    password: str


class IcsAccount(Base):
    """A published, read-only iCalendar feed -- a TripIt private URL, say.

    ``url`` is a credential in its own right: the feeds worth mirroring embed a
    token in the path and are readable by anyone who has it. Nothing may echo it
    into a message or a log line; see ``IcsProvider``.
    """

    type: Literal["ics"]
    url: str


Account = Annotated[GoogleAccount | CaldavAccount | IcsAccount, Field(discriminator="type")]


class Config(Base):
    accounts: dict[str, Account]
    syncs: list[SyncSpec]


def _interpolate(node: Any, env: Mapping[str, str]) -> Any:
    """Substitute ``${VAR}`` inside every string of a parsed YAML structure."""
    if isinstance(node, dict):
        return {key: _interpolate(value, env) for key, value in node.items()}
    if isinstance(node, list):
        return [_interpolate(item, env) for item in node]
    if isinstance(node, str):

        def replace(match: re.Match[str]) -> str:
            name = match.group(1)
            if name not in env:
                raise ConfigError(f"environment variable {name} is not set")
            return env[name]

        return ENV_PATTERN.sub(replace, node)
    return node


def _merge(defaults: dict, override: dict) -> dict:
    """Deep merge where ``override`` wins; nested dicts merge key by key."""
    merged = copy.deepcopy(defaults)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_config(text: str, env: Mapping[str, str]) -> Config:
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError("config must be a YAML mapping")

    raw = _interpolate(raw, env)
    defaults = raw.pop("defaults", None) or {}
    if not isinstance(defaults, dict):
        raise ConfigError("defaults must be a mapping")
    if "syncs" not in raw:
        raise ConfigError("config must define syncs")
    syncs = raw["syncs"]
    if not isinstance(syncs, list):
        raise ConfigError("syncs must be a list")
    if not syncs:
        raise ConfigError("syncs must not be empty")
    for index, sync in enumerate(syncs):
        if not isinstance(sync, dict):
            raise ConfigError(f"syncs[{index}] must be a mapping")
    raw["syncs"] = [_merge(defaults, sync) for sync in syncs]

    try:
        config = Config(**raw)
    except ValidationError as exc:
        # Never render pydantic's ``input_value``: after interpolation it can be a secret.
        details = "; ".join(
            f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
            for error in exc.errors()
        )
        raise ConfigError(details) from exc

    seen: set[str] = set()
    for sync in config.syncs:
        if sync.id in seen:
            raise ConfigError(f"duplicate sync id '{sync.id}'")
        seen.add(sync.id)
        for role in ("source", "dest"):
            ref: CalendarRef = getattr(sync, role)
            if ref.account not in config.accounts:
                raise ConfigError(
                    f"sync '{sync.id}' {role} references unknown account '{ref.account}'"
                )
        # A feed is a static document served over HTTP; there is nothing to write to.
        # Rejected here so the user hears it once at load, rather than on every pass
        # after the source has already been read.
        if isinstance(config.accounts[sync.dest.account], IcsAccount):
            raise ConfigError(
                f"sync '{sync.id}' dest account '{sync.dest.account}' is a read-only "
                "ICS feed; a feed can only be a source"
            )
        if sync.source == sync.dest:
            raise ConfigError(
                f"sync '{sync.id}' has the same source and dest calendar; "
                "it would mirror events back into the source"
            )
    return config


def load_config_file(path: str | Path, env: Mapping[str, str]) -> Config:
    try:
        text = Path(path).read_text()
    except OSError as exc:
        raise ConfigError(f"cannot read config at {path}: {exc}") from exc
    return load_config(text, env)
