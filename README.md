# calsync

calsync mirrors calendar events one way, from a source calendar to a destination
calendar, so that time blocked in one place shows as blocked in another. It speaks
Google Calendar and CalDAV (exercised against iCloud) in either direction, and each
mirror can be a padded, detail-free `Busy` block or a full copy of the source event.
It keeps **no state** — no database, no cache, no volume: every mirror carries the
identity it needs to be matched back to its source on the next pass, so a pass over
unchanged input is a no-op and a source event that disappears simply loses its mirror.
It ships as a Docker container that runs a sync pass on an interval.

## Quick start

```bash
# 1. Copy compose.yml out of this repo and edit the inline config to name
#    your own accounts and calendars.

# 2. Create .env next to it with your secrets.
cat > .env <<'EOF'
ICLOUD_USER=you@icloud.com
ICLOUD_APP_PASSWORD=xxxx-xxxx-xxxx-xxxx
GOOGLE_CLIENT_ID=...apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=...
GOOGLE_REFRESH_TOKEN_WORK=
GOOGLE_REFRESH_TOKEN_PERSONAL=
EOF

# 3. Build the image, then mint a refresh token per Google account (below)
#    and paste each one into .env.
docker compose build

# 4. Check the config parses and the syncs read the way you meant.
docker compose run --rm calsync calsync validate

# 5. See what a pass would do, without writing anything.
docker compose run --rm calsync calsync sync --once --dry-run

# 6. Run it.
docker compose up -d
docker compose logs -f
```

Steps 4 and 5 are worth keeping: `validate` catches config mistakes without touching
a calendar, and `--dry-run` catches the rest before anything is written.

## Google OAuth setup

Personal Gmail accounts are in scope, so a service account with domain-wide delegation
is not an option — the refresh token has to be minted by the human who owns the
calendar. One token per Google account; the client ID and secret are shared.

**Once, in the Google Cloud Console:**

1. Create (or pick) a project.
2. Enable the **Google Calendar API** for it.
3. Configure the OAuth consent screen. While it is in *Testing*, add every Google
   account you intend to mirror as a test user.
4. Create an OAuth client of type **Desktop app**. Copy the client ID and client
   secret into `.env` as `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET`.

A Desktop-app client accepts any loopback redirect port, which is what lets the `auth`
subcommand pick its own port.

**Then, once per Google account:**

```bash
docker run --rm -it -p 8080:8080 --env-file .env calsync:latest \
  calsync auth --env-var GOOGLE_REFRESH_TOKEN_WORK
```

`--client-id` and `--client-secret` default to `GOOGLE_CLIENT_ID` and
`GOOGLE_CLIENT_SECRET` in the environment, which `--env-file .env` supplies.
`--env-var` names the variable the printed line will be pasted into, so the output is
paste-ready as it stands; it defaults to `GOOGLE_REFRESH_TOKEN`. `--port` defaults to
`8080`; publish that same port, because the redirect Google sends the browser to is
`http://localhost:<port>/`.

For the seconds the approval takes, the one-shot server listens on all interfaces
inside the container — that is what lets the published port reach it — and
`--bind 127.0.0.1` restricts it to loopback when you run `calsync auth` outside a
container.

The command prints an authorization URL and waits. Open it in a browser, sign in as
the account you are minting for, approve, and the terminal prints:

```
refresh token minted. Put it in calsync's environment as:

GOOGLE_REFRESH_TOKEN_WORK=1//0g...
```

Paste that line into `.env`. Repeat with `--env-var GOOGLE_REFRESH_TOKEN_PERSONAL`
for the second account, then `docker compose up -d`.

If Google returns no refresh token, it is because the account has already authorized
this client and Google only re-issues on a fresh consent. Revoke the app at
<https://myaccount.google.com/permissions> and run `auth` again; calsync tells you so
rather than handing back an unusable credential.

## iCloud setup

1. Sign in at <https://appleid.apple.com>, and under **Sign-In and Security →
   App-Specific Passwords** generate one for calsync.
2. Put your Apple ID and that password into `.env` as `ICLOUD_USER` and
   `ICLOUD_APP_PASSWORD`. Your regular Apple ID password will not work.
3. Leave `url: https://caldav.icloud.com` alone — the principal and calendar home are
   discovered from it.

The target calendar is matched **by its display name**, case-insensitively: whatever
the calendar is called in the Calendar app is what goes in `calendar:`. Get it wrong
and calsync tells you what it did find:

```
calendar 'Hom' not found; available calendars: Family, Home, Work
```

`ICLOUD_USER` does double duty: it is the login, and it is the address calsync matches
against `ATTENDEE` lines to decide whether *you* declined an event. Without it nothing
is ever treated as declined, on the grounds that over-mirroring is recoverable and
silently dropping your own event is not.

## Configuration reference

The config is YAML with three top-level keys — `accounts`, `defaults`, and `syncs`.
In the shipped `compose.yml` it lives inline under compose's `configs:` key and is
mounted at `/config.yaml`. Unknown keys are rejected everywhere rather than ignored,
so a typo is an error and not a silently missing setting.

calsync reads `/config.yaml` by default; `--config PATH` or the `CALSYNC_CONFIG`
environment variable overrides that.

### `accounts`

A mapping of account name to account. The name is what `syncs` refer to. Every type
requires every field; there are no defaults, because every field is a credential.

| Key | Type | Notes |
| --- | --- | --- |
| `type` | `google` \| `caldav` \| `ics` | Selects the shape of the rest |
| `client_id` | string | **google.** OAuth client ID |
| `client_secret` | string | **google.** OAuth client secret |
| `refresh_token` | string | **google.** From `calsync auth`, one per account |
| `url` | string | **caldav.** e.g. `https://caldav.icloud.com` |
| `username` | string | **caldav.** Apple ID; also used for decline matching |
| `password` | string | **caldav.** App-specific password |
| `url` | string | **ics.** The published feed, e.g. a TripIt private iCalendar URL. `webcal://` and `webcals://` are accepted and fetched over https. |

#### `ics` feeds

```yaml
accounts:
  trips:
    type: ics
    url: ${TRIPIT_FEED_URL}
```

**The feed URL is a credential.** A published feed embeds a token — TripIt puts one in
the path, other providers put it in a query string — and anyone holding the URL can
read the whole calendar. Keep it in an environment variable like any other secret;
calsync never writes it to a log or an error message, which name the feed by host
only (`https://www.tripit.com/...`).

An `ics` account is **source-only**. A feed is a static document with nothing to write
to, so config rejects one used as a sync's `dest`. `source.calendar` is still required
by the schema but is ignored: the URL already names exactly one calendar, so any value
there selects the same thing.

Recurrences are **expanded client-side**, because a feed has no server to ask for
occurrences the way Google and CalDAV do. That makes the feed, not calsync, the thing
deciding how much work a pass does, so expansion is capped at 5000 occurrences per
sync window; a feed that exceeds it fails the pass rather than writing a runaway
series onto the destination. The response is also capped at 20 MB and at a total
30-second transfer budget.

### `defaults`

Optional. Any `syncs` field may appear here and is deep-merged into every sync, with
the sync winning key by key. The merge descends into nested mappings, so a sync that
sets `padding: {before: 15m}` keeps the default `after`.

### `syncs`

A non-empty list. Each entry:

| Key | Default | Notes |
| --- | --- | --- |
| `id` | *required* | Stable, unique, and part of every mirror's identity. Must match `[A-Za-z0-9][A-Za-z0-9_-]*` — letters, digits, `_` and `-`, first character not `_` or `-`. See [How it works without a database](#how-it-works-without-a-database) before renaming one. |
| `source.account` | *required* | An `accounts` key |
| `source.calendar` | *required* | Google: the calendar ID (`primary`, or the calendar's address). CalDAV: the calendar's display name. |
| `dest.account` | *required* | An `accounts` key |
| `dest.calendar` | *required* | Same rule as `source.calendar`. The `dest` pair must not equal the `source` pair, or the sync would mirror into itself. |
| `window.past` | `1d` | How far back to look. Events outside the window are never touched. |
| `window.future` | `14d` | How far ahead to look |
| `padding.before` | `0m` | Shifts the mirror's start earlier. Stacks on top of any travel time the source records — see [Operating notes](#operating-notes). |
| `padding.after` | `0m` | Shifts the mirror's end later |
| `privacy` | `busy` | `busy` copies no detail: the title becomes `title`, and description, location, and the all-day flag are dropped. `full` copies title, description, location, and the all-day flag verbatim. |
| `title` | `Busy` | The mirror's title under `privacy: busy`. Ignored under `full`. |
| `skip.declined` | `true` | Skip events *this account* has declined |
| `skip.free` | `true` | Skip events that do not block time (`TRANSP:TRANSPARENT`, Google `transparency: transparent`) |
| `skip.all_day` | `true` | Skip all-day events, so birthdays and holidays do not become full-day busy blocks |
| `skip.cancelled` | `true` | Skip `STATUS:CANCELLED` events. A source event that becomes cancelled loses its mirror. |
| `skip.mirrors` | `true` | The loop guard: never re-mirror an event that already carries calsync identity, so `A→B` and `B→A` can coexist |
| `max_deletes_per_pass` | `20` | The delete rail; must be `>= 0`, and `0` makes any deletion at all trip it. See [Operating notes](#operating-notes). |

Neither privacy mode copies attendees, attachments, conferencing links, or reminders —
Google mirrors are written with reminders explicitly disabled, so a mirrored day does
not notify you twice. Every mirror is written opaque, which is the point of mirroring
at all.

### Duration syntax

`window.*` and `padding.*` take a whole number and one unit suffix: `s`, `m`, `h`,
or `d` — `30s`, `15m`, `2h`, `14d`. A bare number is read as seconds. Negative values
are rejected. There is no compound form: write `90m`, not `1h30m`.

### `${VAR}` interpolation

Every string in the config is scanned for `${NAME}` and substituted from the process
environment. A referenced variable that is not set is an error, not an empty string —
a blank password would otherwise reach the server as a real authentication attempt.
There is no default-value syntax.

**Inside `compose.yml`'s inline config, write `$${VAR}`, not `${VAR}`.** Compose
interpolates the `configs:` block itself before the container ever sees it, and `$$`
is compose's escape: it emits a literal `${VAR}` into `/config.yaml` for calsync to
expand at startup, against the variables listed under `environment:`. Written with a
single `$`, compose would substitute it from *your shell's* environment at
`docker compose up` time, and the container would receive a config with the secret
baked in — or, more likely, with an empty string where the secret should be.

### Environment variables

| Variable | Default | Read by |
| --- | --- | --- |
| `CALSYNC_CONFIG` | `/config.yaml` | Default for `--config` on `sync` and `validate` |
| `SYNC_INTERVAL` | `15m` | The sync loop, and the healthcheck's staleness threshold |
| `LOG_LEVEL` | `info` | Logging, on stdout |
| `TZ` | container default | Only affects log timestamps; all calendar arithmetic is UTC |
| `GOOGLE_CLIENT_ID` | — | Default for `calsync auth --client-id` |
| `GOOGLE_CLIENT_SECRET` | — | Default for `calsync auth --client-secret` |

Everything else — the account credentials — is referenced by name from the config,
so you choose those variable names yourself.

## How it works without a database

Each mirror is identified by a key derived only from things that are stable across
passes:

```
key = sha256(sync_id + "\0" + source_uid + "\0" + instance_start_utc).hexdigest()[:16]
```

and it carries a `hash` over the content that should trigger an update: the padded
start and end, the title, the description with calsync's own marker line removed, the
location, and whether it is all-day. Recompute both from the source, compare against
what the destination's mirrors carry, and the diff falls out: a key only in the
desired set is a **create**, a key in both whose stored hash differs is an **update**,
a key only on the destination is a **delete**. No stored state is consulted, so the
same input twice produces zero operations.

That only works if the identity survives a round trip through the server, so calsync
writes it in three places and reads them back in priority order, field by field:

1. **Backend-native properties**, which are what calsync wrote last and therefore win.
   On Google, `extendedProperties.private` — `calsync_sync`, `calsync_key`,
   `calsync_hash`; these are also server-side queryable, and `list_mirrors` filters on
   `calsync_sync` rather than fetching everything. On CalDAV, the `X-CALSYNC-SYNC`,
   `X-CALSYNC-KEY` and `X-CALSYNC-HASH` properties on the VEVENT.
2. **The mirror's UID**, `calsync-<sync_id>-<key>@calsync`, which carries the sync id
   and the key. This is a CalDAV channel: a UID is the object's identity on every
   CalDAV server and survives anything short of a re-create. (Google assigns its own
   event ids, so on Google this channel does not exist.)
3. **A marker line in the description**, `[calsync:<key>:<hash>]`, on its own line,
   which carries the key and the hash. Description text survives a server that drops
   unknown properties — iCloud does exactly that on some calendars.

Channels 2 and 3 exist because they do not depend on the server preserving anything
custom, and between them they cover all three fields. That matters more than it
sounds: a mirror whose identity cannot be read is invisible to its own sync, so every
pass re-creates it and no pass ever reaps it, and the destination fills with
duplicates. The UID outranks the marker line for the key, because a UID is structural
and a description is prose a user can edit. A missing hash is treated as *unknown*,
which forces one update — far cheaper than not recognising the mirror at all.

The marker line is matched anchored to a whole line, so a marker quoted inside your
own prose is not mistaken for calsync identity and does not get your event classified
as a mirror and silently skipped.

### Renaming a sync id orphans its mirrors

The sync id is an input to the key, so changing it changes every key that sync
derives — and it is also what `list_mirrors` filters on. The mirrors created under the
old id therefore fall out of the renamed sync's view entirely: they are never returned,
so they are never updated and never deleted, and the loop guard keeps anything else
from adopting them. They just sit on the destination forever, alongside a fresh set
created under the new id.

**Delete them by hand before or after the rename.** `calsync validate` cannot help
here — it parses the config and never contacts a calendar, so it has no way to know
what mirrors already exist. Treat a sync id as permanent; if you must rename one,
`--dry-run` the new config first and expect to see a create for every event with no
matching delete.

## Operating notes

```bash
docker compose logs -f                                   # follow
docker compose run --rm calsync calsync sync --once --dry-run   # plan only
docker compose run --rm calsync calsync validate         # parse the config only
```

**Dry-run before anything risky.** `--dry-run` runs one real pass — it reads both
calendars — and prints per sync what it would do, writing nothing:

```
personal-to-work: would create=3 would update=1 would delete=0 skipped=12
work-to-shared: would create=0 would update=0 would delete=0 skipped=4
```

Without `--dry-run`, `--once` runs a single pass, applies it, and exits; the exit
status is non-zero if any sync failed. Either flag on its own means a single pass —
plain `calsync sync` is the one that loops. Creates and updates are applied before
deletes, so an event that moved never leaves you looking free in between.

**The delete rail.** `max_deletes_per_pass` (default 20) caps how many mirrors a
single sync may remove in one pass. If the diff wants more, the sync aborts *before
applying anything at all* — no creates, no updates, no deletes — and logs the count.
It exists because a source fetch that returns empty on a masked failure looks exactly
like "the user deleted everything", and the unguarded response would be to wipe the
destination. Tripping it means one of: you really did clear a large part of the source
(raise the limit for a pass, or wait it out one sync at a time), you shrank
`window.future` a long way, or the source read is lying. Check the source calendar
before raising the number.

The rail also covers duplicate reaping. If a key ever ends up on two mirrors — a
provider retry after a write that actually committed, or two calsync instances
racing — the extras are queued for deletion so the destination converges on one
mirror per key, and those deletions count against the limit.

**The healthcheck.** The loop touches a heartbeat file (`/tmp/calsync-heartbeat` by
default, `--heartbeat` to move it) after a pass in which *every* sync succeeded. The
container healthcheck fails if that file is missing or older than twice
`SYNC_INTERVAL`. So a container that is permanently unhealthy means **at least one
sync is failing every pass** — not that syncing has stopped. The others are still
running and still writing. `docker compose logs` names the failing sync; a sync
failure never aborts the others in the pass. Providers are rebuilt each pass and
closed at the end of it.

If you move the heartbeat with `sync --heartbeat`, give `healthcheck --heartbeat` the
same path — the image's `HEALTHCHECK` uses the default, and a container whose loop
writes somewhere else is permanently unhealthy while syncing perfectly. A heartbeat
that cannot be written at all (read-only mount, missing parent directory) is logged
rather than raised, so it degrades the healthcheck instead of crash-looping a
container whose syncs work.

**What gets skipped by default.** All-day events, events you have declined, events
that do not block time, and anything already carrying calsync identity. If a sync is
mirroring less than you expect, the `skipped=` count in the summary is the first place
to look, and the four `skip.*` flags are the knobs.

**Google event types.** Google puts several `eventType`s on a calendar that do not
actually block time: `workingLocation`, `birthday`, and `fromGmail`. calsync reads
those as free, so the default `skip.free` drops them and your destination does not
fill with daily noise. `focusTime` and `outOfOffice` are deliberately *not* in that
set — they do block time, and they are mirrored.

**Travel time.** Apple Calendar records the journey to an event in a property of
its own (`X-APPLE-TRAVEL-DURATION`) and leaves the event's own start alone, so an
iCloud event read from its start and end alone showed you free while you were still
driving. calsync subtracts that travel time from the mirror's start, on top of
`padding.before`: a 09:00–10:00 event 90 minutes away, with `padding.before: 15m`
and `padding.after: 15m`, is mirrored as 07:15–10:15. It is *lead* time only —
Apple records no return journey, so nothing is added after the event — and it is
ignored on an all-day source, where a lead time says nothing useful and would force
the block off midnight. There is nothing to switch on: every CalDAV and ICS source
contributes it. A Google source contributes none, because Google Calendar has no
such data — travel time there is a Maps feature of the web UI, not part of the
event. Mirrors themselves never carry the property, so the lead time is baked into
the mirror's start exactly once and cannot be added again by a second sync reading
that mirror.

**All-day events.** Internally an all-day event is UTC midnight to UTC midnight, and
it is written back in the backend's native all-day form (a `date` value on Google, a
DATE-valued `DTSTART` on CalDAV) so it does not render a day early west of Greenwich.
Two things change that: `privacy: busy` drops the all-day flag, so a busy mirror is
always a timed block; and padding that moves an edge off midnight cannot be expressed
as a date, so the mirror falls back to a timed block. Both only arise if you turn
`skip.all_day` off.

**Manual edits to a mirror are permanent.** The differ compares the hash *stored on
the mirror* against the hash it recomputes from the source. Editing a mirror on the
destination — retitling it, moving it — changes neither hash, so no update is issued
and the edit is never repaired. Mirrors are outputs; edit the source. To undo an edit,
delete the mirror and let the next pass re-create it.

**Retries.** Transient backend failures — 429, 500, 502, 503, 504, connection
timeouts, and CalDAV rate limiting — are attempted up to four times, with exponential
backoff and jitter between tries. Google's 403 is *not* blanket-retried: it means
quota exhaustion only when the response reason says so, and otherwise it is a standing
condition (no access to the calendar, the Calendar API not enabled, a suspended
account) that would burn backoff on every pass forever. Deleting an event that is
already gone counts as success; every other delete failure surfaces.

**Secrets.** The only place calsync prints a secret is the last line of `calsync
auth`, deliberately, because that is the one moment you can capture it. Config
validation errors never echo the offending value, and connection failures have the
credential redacted out of the library's own error text.

## Known limitations

**`urllib3` and `urllib3-future` coexist in the image.** `caldav` pulls in `niquests`,
which pulls in `urllib3-future`; the Google stack pulls in `requests`, which pulls in
stock `urllib3`. Both claim `site-packages/urllib3`, and `urllib3-future` ships a
`.pth` file that repairs the clash by rewriting that directory when the interpreter
starts — but only when the directory is writable. The runtime image runs as a non-root
user over a root-owned environment, so that repair can never run there, and a
half-merged tree would be frozen into the image. The Dockerfile settles the tree in
the build stage while it is still writable and then fails the build on
`python -c "import calsync.cli"` if imports are still broken. The consequences: do not
install into the environment at runtime, and do not assume a plain non-root
`pip install` of these dependencies will work — it is the build-time import check that
makes the combination safe.

**Floating times are read as UTC.** An iCal time with no timezone means "local time
wherever the event is viewed" (RFC 5545), which calsync cannot resolve: it keeps no
state and does not know the calendar's zone. It reads such times as UTC. The mirror is
therefore off by the viewer's UTC offset. Reading them as the host's local zone was
the alternative, and it is worse — it would make the mirror key host- and
DST-dependent, so the same occurrence would re-key, and thus be deleted and
re-created, whenever the host moved or the clocks changed. UTC is wrong by a fixed
offset; local is wrong unpredictably.

**One way, one event at a time.** Overlapping mirrors are left overlapping rather than
merged into a single block — the strict 1:1 mapping between a source occurrence and
its mirror is what keeps identity derivable and diffs cheap. Bidirectional behaviour
is two syncs, which the loop guard keeps from ping-ponging.

**Windowed, not complete.** Only the configured window is read or written. An event
that moves beyond `window.future` loses its mirror; moved back in, it gains one again.
Both are correct, but a long-horizon event is simply not mirrored until it comes into
range.

**Recurring events are expanded into instances.** Each occurrence is a separate mirror
with its own key, expanded server-side. Editing the series on the source is reflected
occurrence by occurrence; the mirrors are never themselves a recurring event.

**No attendees, attachments, conferencing links, or reminders**, under either privacy
mode.

## Development

```bash
pixi run test    # pytest
pixi run lint    # ruff check + ruff format --check
pixi run fmt     # ruff format
```

The suite runs entirely offline — 395 tests in well under a second. Sync behaviour is
exercised through an in-memory `FakeProvider` that implements the same Protocol as the
real backends, and the provider parsing tests drive the real Google and CalDAV code
against literal API payloads and iCalendar text. Nothing in CI opens a socket.

To build and check the image:

```bash
docker compose build
docker compose run --rm calsync calsync validate
```

## License

MIT — see [LICENSE](LICENSE).
