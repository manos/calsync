# calsync — Design

Date: 2026-08-24

## Purpose

Mirror calendar events between accounts so that time blocked in one calendar shows
as blocked in another. Primary use cases:

1. **iCloud → Gmail (work)** — personal commitments appear on the work calendar as
   opaque "Busy" blocks, optionally padded, with no personal detail leaked.
2. **Gmail (work) → Gmail (shared personal)** — work commitments appear on a shared
   household calendar, optionally with full detail.

Runs as a Docker container. All configuration lives in `compose.yml`; credentials
arrive as environment variables. The tool keeps **no state store** — no database,
no cache, no volumes.

## Scope

In scope:

- Reading and writing both Google Calendar and CalDAV (iCloud) calendars, in either
  direction.
- One-way mirroring per configured sync. Bidirectional behaviour is achieved by
  configuring two syncs, which the loop guard keeps from ping-ponging.
- Privacy transform ("Busy" only) and time padding.
- Propagating creates, updates, and deletes.

Out of scope:

- Merging overlapping blocks into a single event.
- Copying attendees, attachments, conferencing links, or reminders.
- Any web UI, API, or persistent storage.

## Stack

Python 3.13. `caldav` + `icalendar` for CalDAV (most mature handling of iCloud's
quirks), `google-auth` + `google-api-python-client` for Google Calendar, `pydantic`
for config validation, `pytest` for tests. The workload is protocol correctness,
not throughput, so library maturity dominates the choice.

## Architecture

Modules, each independently testable:

| Module | Responsibility |
| --- | --- |
| `config.py` | Load YAML, interpolate `${ENV}`, validate with pydantic |
| `model.py` | Normalized, timezone-aware `CalEvent` shared by all providers |
| `marker.py` | Derive the mirror key and content hash; write and read every identity channel |
| `providers/base.py` | Provider Protocol, `ProviderError`/`TransientError` |
| `providers/google.py` | Google Calendar implementation |
| `providers/caldav.py` | CalDAV/iCloud implementation |
| `providers/factory.py` | The only module that opens a connection: accounts in, providers out |
| `providers/fake.py` | In-memory Provider, so the whole loop is testable offline |
| `retry.py` | Exponential backoff with jitter, on `TransientError` only |
| `transform.py` | Filters, padding, privacy transform, desired-mirror construction |
| `sync.py` | Diff desired vs. existing mirrors; emit and apply operations |
| `runner.py` | Run every sync once or on an interval; heartbeat; provider lifetime |
| `cli.py` | `sync [--once] [--dry-run]`, `validate`, `auth`, `healthcheck` |

The provider Protocol:

```python
list_events(window) -> list[CalEvent]
list_mirrors(window, sync_id) -> list[CalEvent]
create(event) -> None
update(event) -> None
delete(event) -> None
```

Writes take an event rather than a key: the backend-native identifier lives on
`CalEvent.uid`, and any further addressing detail — a CalDAV href — rides in `raw`,
which the differ copies from the existing mirror onto the desired content. A provider
holding a connection may also expose `close()`; the runner calls it if it is there.

Adding a backend means implementing that Protocol and nothing else. `transform.py`
and `sync.py` never import a concrete provider, and only `providers/factory.py` ever
holds a credential.

## Stateless identity

Every mirrored event carries a deterministic key:

```
key = sha256(sync_id + source_uid + instance_start_utc)[:16]
```

Identity is written in three channels and read back per field, in this order:

- **Backend-native properties**, preferred because they are what calsync wrote last.
  On Google, `extendedProperties.private` — `calsync_sync`, `calsync_key`,
  `calsync_hash` — which are also queryable server-side via the
  `privateExtendedProperty` filter, so `list_mirrors` narrows on the server.
  On CalDAV, `X-CALSYNC-SYNC`, `X-CALSYNC-KEY`, `X-CALSYNC-HASH` on the VEVENT.
- **The UID**, `calsync-<sync_id>-<key>@calsync`, carrying *both* the sync id and the
  key. A CalDAV channel: a UID is the object's identity on every CalDAV server, while
  Google assigns event ids itself.
- **A description line**, `[calsync:<key>:<hash>]`, on a line of its own, carrying
  *both* the key and the hash. Prose survives a server that strips unknown `X-`
  properties, which iCloud does on some calendars.

The two fallback channels each carry a second field deliberately. Carrying only the
key made a mirror whose properties went missing invisible to its own sync: the key
was recoverable but the sync id and the hash were not, so every pass re-created the
mirror and no pass could reap it, and the destination filled with duplicates without
bound. Between the UID and the description line all three fields survive the loss of
custom properties. The UID outranks the description for the key — a UID is
structural, a description is prose a user can edit. A hash that is still unknown
after all three channels forces a single update, which is far cheaper than failing to
recognise the mirror at all.

Matching the description line is anchored to a whole line, so a marker quoted inside a
user's own text is not read as calsync identity — which would classify their event as
a mirror and silently exclude it from syncing.

`calsync_hash` covers the padded start and end, the title, the description with the
marker line stripped, the location, and the all-day flag. It makes a repeated pass a
no-op without storing anything. Because it is compared *as stored* against the hash
recomputed from the source, a user's manual edit to a mirror changes neither side and
is never repaired: mirrors are outputs, not shared documents.

## A sync pass

Per configured sync, within the window:

1. Fetch source events, recurrences expanded server-side into individual instances.
2. Drop filtered events (see below).
3. Transform survivors into desired mirror events.
4. Fetch existing mirrors on the destination for this `sync_id`, indexing by key.
   A key that appears twice — a retry after a write that in fact committed, or two
   instances racing — keeps the first and queues the rest for deletion, so a duplicate
   is reapable rather than permanently invisible.
5. Diff by key:
   - in desired, not in existing → **create**
   - in both, `calsync_hash` differs → **update**
   - in existing, not in desired → **delete**
6. Apply, subject to the delete rail. Creates and updates land before deletes: a moved
   event is a create plus a delete of the old mirror, and deleting first would show the
   user free in between.

Providers are built once per pass and closed at the end of it, so a long-lived
container does not leak a connection pool per calendar per pass. A provider that
refuses to hang up is logged and stepped over rather than failing the pass.

Deletions therefore propagate with zero stored state: an event that vanished from
the source simply has no desired key this pass.

Events outside the window are never touched. A source event moved beyond the window
loses its mirror (correct); moved into the window, it gains one (correct).

## Filters

Each is a per-sync toggle, all defaulting to `true`:

- **declined** — events *this account* has declined, matched by the account's own
  address; a colleague's decline is not the user's
- **free** — `transparency: transparent`, plus the Google `eventType`s that reserve no
  time (`workingLocation`, `birthday`, `fromGmail`). `focusTime` and `outOfOffice` are
  excluded from that set: they do block time and must still be mirrored.
- **all_day** — otherwise every birthday and holiday becomes a full-day busy block
- **cancelled** — `STATUS:CANCELLED`; any existing mirror is deleted
- **mirrors** — the loop guard: a source event carrying any calsync marker is never
  re-mirrored, so `A→B` and `B→A` can coexist safely

## Transform

- **Padding:** `before` and `after` durations shift the mirror's start and end.
  Overlapping results are left overlapping — strict 1:1 mapping between a source
  event and its mirror keeps identity trivial and diffs cheap.
- **Privacy `busy`:** title replaced with the configured string (default `"Busy"`);
  description, location, attendees, and the all-day flag dropped, so a busy mirror is
  always a timed block.
- **Privacy `full`:** title, description, and location copied verbatim.
- All times normalized to timezone-aware UTC internally and written with an
  explicit timezone. An all-day event is UTC midnight to UTC midnight, and is written
  back in the backend's date form so it does not render a day early west of Greenwich;
  padding that moves an edge off midnight falls back to a timed block, because a date
  cannot express it.
- A floating (timezone-less) iCal time is read as UTC. RFC 5545 makes it mean "local
  time wherever viewed", which a stateless tool cannot resolve; reading it as the
  host's zone would make the key host- and DST-dependent, re-keying the same
  occurrence whenever the host moved. UTC is wrong by a fixed offset, local is wrong
  unpredictably.

## Delete rail

`max_deletes_per_pass` (default 20). If a sync's diff wants more deletions than
that — including the duplicates queued for reaping — the sync aborts before applying
anything and logs at error level. This guards against a source fetch that returns
empty on a masked failure, which would otherwise wipe the destination's mirrors.

## Configuration

Config lives inline in `compose.yml` under the top-level `configs:` key and is
mounted at `/config.yaml`. Only secrets and runtime knobs come from `environment:`.

```yaml
accounts:
  icloud_personal:
    type: caldav
    url: https://caldav.icloud.com
    username: ${ICLOUD_USER}
    password: ${ICLOUD_APP_PASSWORD}      # app-specific password
  gmail_work:
    type: google
    client_id: ${GOOGLE_CLIENT_ID}
    client_secret: ${GOOGLE_CLIENT_SECRET}
    refresh_token: ${GOOGLE_REFRESH_TOKEN_WORK}
  gmail_personal:
    type: google
    client_id: ${GOOGLE_CLIENT_ID}
    client_secret: ${GOOGLE_CLIENT_SECRET}
    refresh_token: ${GOOGLE_REFRESH_TOKEN_PERSONAL}

defaults:
  window: {past: 1d, future: 14d}
  padding: {before: 0m, after: 0m}
  privacy: busy                    # busy | full
  skip: {declined: true, free: true, all_day: true, cancelled: true, mirrors: true}
  max_deletes_per_pass: 20

syncs:
  - id: personal-to-work           # stable and unique: it is part of the identity key
    source: {account: icloud_personal, calendar: "Home"}   # CalDAV: display name
    dest:   {account: gmail_work, calendar: primary}
    privacy: busy
    title: "Busy"
    padding: {before: 15m, after: 15m}

  - id: work-to-shared
    source: {account: gmail_work, calendar: primary}
    dest:   {account: gmail_personal, calendar: "Shared"}
    privacy: full
```

A sync `id` is constrained to `[A-Za-z0-9][A-Za-z0-9_-]*`, because it is embedded in
every mirror's UID and has to be recoverable from it; a leading `-` or `_` would make
the split against the fixed-width key ambiguous. Renaming an `id` changes every
derived key *and* the value `list_mirrors` filters on, so that sync's existing mirrors
fall out of its view: never returned, so never updated and never deleted. They must be
removed by hand. `calsync validate` cannot detect this — it parses the config and
never contacts a calendar, so it has no view of what already exists.

Values written as `${VAR}` are substituted from the environment, and a variable that
is not set is an error rather than an empty string. Inside compose's inline `configs:`
block the same reference is written `$${VAR}`, so compose passes the literal through
for calsync to expand in the container rather than substituting it on the host.

Environment variables: the secrets referenced above, plus `CALSYNC_CONFIG` (default
`/config.yaml`), `SYNC_INTERVAL` (default `15m`), `LOG_LEVEL` (default `info`), and
`TZ`.

## Authentication

- **Google:** OAuth2 refresh tokens, one per account. Service accounts with
  domain-wide delegation are unusable here because personal Gmail accounts are in
  scope. `calsync auth` runs a one-shot loopback OAuth server, prints the
  authorization URL rather than opening a browser, and on approval prints the refresh
  token together with the name of the variable to paste it into — `--env-var`, since
  one variable per account is what makes the printed line paste-ready. The server
  listens on all interfaces so a published port reaches it from the host, while the
  redirect URI stays `http://localhost:<port>`; `--bind` narrows the interface when
  the command is run outside a container. Scope:
  `https://www.googleapis.com/auth/calendar`.
- **iCloud:** an app-specific password over CalDAV. The principal and calendar home
  are discovered from `https://caldav.icloud.com`; the target calendar is matched by
  display name.

## Runtime

A `pixi` build stage over a `debian:bookworm-slim` runtime, non-root user, no volumes,
`restart: unless-stopped`. The build stage also settles the `urllib3` /
`urllib3-future` clash that `requests` and `niquests` create between them — the repair
`.pth` needs a writable tree, which the non-root runtime is not — and fails the build
on an import check if the tree is still broken. The default command loops: sync every
`SYNC_INTERVAL`, log a one-line summary per sync per pass. `--once` runs a single pass
and exits, for manual or cron use.

A healthcheck reads a heartbeat file in `/tmp`, touched after a pass in which *every*
sync succeeded, and fails if it is older than twice the interval. The file is
ephemeral scratch, not a data store. A permanently unhealthy container therefore means
one sync failing every pass, not that syncing has stopped; the loop logs which. A
heartbeat that cannot be written is logged rather than raised, so it never crash-loops
a container whose syncs are working.

## Error handling

- Syncs are isolated: one failing sync never aborts the others in a pass.
- Exponential backoff with jitter on HTTP 429, 5xx, and connection failures. Google's
  403 is *not* blanket-transient: it is quota exhaustion only when the response reason
  says so, and otherwise a standing condition — no access to the calendar, the API not
  enabled, a suspended account — that would burn backoff on every pass forever.
- Config errors fail fast with a non-zero exit in every mode: the config is loaded
  before the loop is entered. Credential and connection errors surface per sync — a
  non-zero exit under `--once`, and in loop mode a logged failure retried on the next
  interval.
- Errors never echo a secret: pydantic's offending input value is suppressed, and
  connection failures have the credential redacted out of the library's own message.
- `--dry-run` prints the planned creates, updates, and deletes per sync and writes
  nothing. It is a real pass — both calendars are read.

## Testing

Behaviour-level tests against an in-memory `FakeProvider` implementing the Protocol,
so the full sync loop is exercised without network access:

- padding arithmetic, including across a DST boundary
- each filter, individually
- create, update, and delete propagation
- loop guard: a mirrored event is never re-mirrored
- idempotency: a second pass over unchanged input produces zero operations
- delete rail trips and aborts the sync without applying anything
- a mirror stripped of its custom properties is still recognised, from the UID and the
  description line, and is not duplicated

Provider parsing is tested by driving the real Google and CalDAV code against literal
API payloads and iCalendar documents held in the test modules. CI never touches the
network.
