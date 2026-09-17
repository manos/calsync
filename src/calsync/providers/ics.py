"""Read-only provider for a published iCalendar feed (a TripIt private URL, say)."""

from __future__ import annotations

import datetime as dt
import gzip
import http.client
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable

import recurring_ical_events
from icalendar import Calendar as ICalendar
from icalendar import Event as IEvent

from calsync.marker import strip_marker
from calsync.model import CalEvent
from calsync.providers.base import ProviderError, TransientError, Window
from calsync.providers.ical import read_marker, span, text, travel_before
from calsync.retry import retry_call

TRANSIENT_STATUSES = {429, 500, 502, 503, 504}
TIMEOUT = 30.0
# Python's default (``Python-urllib/3.x``) is refused outright by some CDNs, which
# turns a working feed into a permanent 403. Deliberately carries no URL.
USER_AGENT = "calsync"

# A feed is the first source calsync does not control, so the feed decides how much
# work calsync does. Both of these are ceilings on that, not tuning knobs.
#
# A fortnight of ``FREQ=SECONDLY`` is over a million occurrences -- measured at 14 GB
# resident and five minutes -- and each one would be a create against the user's real
# calendar, which the delete rail does not cover. 5000 is far above any real calendar
# in a sync window and far below the point where it costs anything.
MAX_OCCURRENCES = 5000
# A 5.6 MB feed cost 415 MB resident to parse. 20 MB is several times the largest
# real calendar export and still bounded.
MAX_FEED_BYTES = 20 * 1024 * 1024
READ_CHUNK_BYTES = 64 * 1024
GZIP_MAGIC = b"\x1f\x8b"
# The longest run of feed-controlled text allowed into a single log line.
MAX_DETAIL = 200

# Scrubbing is an allowlist, not a denylist: a denylist built from the configured URL
# can only remove strings calsync has already seen, and the two proven leaks are both
# strings it has not -- urllib's "Redirection to url '<target>' is not allowed", which
# quotes the *redirect target*, and a second hop's 404 reason phrase, which quotes the
# redirected path. So anything shaped like a URL, or like a long opaque path or query
# run, is removed wherever it came from.
URL_PATTERN = re.compile(r"[a-z][a-z0-9+.-]*://\S+", re.IGNORECASE)
QUERY_PATTERN = re.compile(r"\?\S+")
PATH_PATTERN = re.compile(r"/\S{8,}")


def scrub(message: object) -> str:
    """Strip anything URL-shaped out of externally-sourced text, and bound its length.

    Applied to every string calsync did not write itself before it is interpolated
    into a message -- exception text from urllib, from icalendar, from a feed's own
    body. The caller adds the host afterwards, so the host is never scrubbed away.
    """
    detail = str(message)
    # URLs first: the whole thing goes, query and all. Then the two halves that can
    # appear without a scheme to recognise them by.
    for pattern in (URL_PATTERN, QUERY_PATTERN, PATH_PATTERN):
        detail = pattern.sub("...", detail)
    # A feed controls the length of what it makes calsync quote, as well as the text.
    return detail[:MAX_DETAIL]


def _decode_body(data: bytes) -> bytes:
    """Undo a compressed body, whatever the server was asked for.

    ``Accept-Encoding: identity`` is a request, not a guarantee. Left compressed,
    the body is a permanent "not parseable iCalendar" that retrying never clears
    and no operator can diagnose from the log.
    """
    return gzip.decompress(data) if data.startswith(GZIP_MAGIC) else data


def fetch_url(url: str, timeout: float = TIMEOUT) -> bytes:
    """GET the feed, under a total time budget and a size ceiling.

    ``timeout=`` bounds a single socket read, not the transfer: a server dripping
    a byte at a time resets that clock forever, and ``retry_call`` then multiplies
    the hang by four. The deadline here is wall-clock and covers the whole body.

    Neither failure names the URL: the caller adds the host, which is the only part
    of a feed URL that is safe to print.
    """
    request = urllib.request.Request(
        url,
        # Identity so the body arrives as iCalendar rather than as gzip; _decode_body
        # handles the servers that ignore it.
        headers={"User-Agent": USER_AGENT, "Accept-Encoding": "identity"},
    )
    deadline = time.monotonic() + timeout
    chunks: list[bytes] = []
    size = 0
    with urllib.request.urlopen(request, timeout=timeout) as response:
        while True:
            chunk = response.read(READ_CHUNK_BYTES)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_FEED_BYTES:
                raise ProviderError(
                    f"the feed is larger than the {MAX_FEED_BYTES} byte ceiling"
                ) from None
            chunks.append(chunk)
            if time.monotonic() >= deadline:
                raise TransientError(f"the feed was still sending after {timeout:g}s") from None
    return _decode_body(b"".join(chunks))


def https_url(url: str) -> str:
    """``webcal://`` is a subscribe scheme; on the wire it is plain https."""
    scheme, separator, rest = url.partition("://")
    if separator and scheme.lower() in ("webcal", "webcals"):
        return f"https://{rest}"
    return url


def redact(url: str) -> str:
    """Scheme and host only -- everything after the host is a credential.

    A private feed URL is readable by anyone who has it: TripIt puts a token in
    the path. The host is the useful half of a failure message ("which feed?"),
    and it is the half that is safe to print.
    """
    parts = urllib.parse.urlsplit(url)
    host = parts.hostname or ""
    if not host:
        return "the ICS feed"
    port = f":{parts.port}" if parts.port else ""
    return f"{parts.scheme}://{host}{port}/..."


class IcsProvider:
    """Read one published iCalendar feed.

    A feed is a static document, so calsync expands recurrences itself -- there
    is no server to ask for occurrences the way CalDAV and Google are asked.
    That makes the feed, not calsync, the thing deciding how much work a pass
    does, which is why the expansion is capped.

    Nothing here is stateful: the document is fetched afresh on every
    ``list_events`` because the feed is the only copy of the truth.
    """

    def __init__(
        self,
        url: str,
        name: str = "ics",
        fetch: Callable[[str], bytes] = fetch_url,
    ) -> None:
        self._url = https_url(url)
        # Precomputed so that every message in this class has a safe thing to name
        # the feed by, and no code path has to remember to redact. It is written by
        # calsync, from the host only, so it is never itself passed through scrub().
        self._label = redact(self._url)
        self._fetch = fetch
        self.name = name

    # -- reading -------------------------------------------------------------

    def list_events(self, window: Window) -> list[CalEvent]:
        events = self._expand(self._read(), window)
        # The Provider contract promises start order; a feed is in whatever order it
        # was written, and expansion interleaves one series with the next.
        events.sort(key=lambda event: event.start)
        return events

    def _expand(self, calendar: ICalendar, window: Window) -> list[CalEvent]:
        """Occurrences overlapping the window, built one at a time and capped.

        Expanding the whole window in one call would materialise every occurrence
        before calsync could count them, which is the failure this guards: the
        cap has to be reached while the occurrences are still being produced.
        ``after()`` yields in start order, so the first occurrence starting at or
        past the window's end is the end of the walk.
        """
        start, end = window
        events: list[CalEvent] = []
        # A static document carries no expanded occurrences, so calsync expands the
        # RRULE/EXDATE/RECURRENCE-ID set itself. One malformed series or event must
        # not escape as a bare KeyError/ValueError through the backend-agnostic
        # Provider contract and take the whole pass down.
        try:
            for component in recurring_ical_events.of(calendar).after(start):
                event = _to_event(component)
                if event.start >= end:
                    break
                events.append(event)
                if len(events) > MAX_OCCURRENCES:
                    # Raised, not truncated: half a feed read as the whole feed would
                    # make every occurrence past the cap look destination-only and
                    # queue it for deletion on the next pass.
                    raise ProviderError(
                        f"{self._label} expands to more than {MAX_OCCURRENCES} "
                        f"occurrences in the sync window; refusing to mirror it"
                    )
        except (KeyError, ValueError, TypeError) as exc:
            raise ProviderError(f"unusable event in {self._label}: {scrub(exc)}") from None
        return events

    def list_mirrors(self, window: Window, sync_id: str) -> list[CalEvent]:
        """Always empty: calsync cannot have written to a feed, so it owns nothing here."""
        return []

    # -- writing, which a feed cannot do -------------------------------------

    def create(self, event: CalEvent) -> None:
        self._refuse("create")

    def update(self, event: CalEvent) -> None:
        self._refuse("update")

    def delete(self, event: CalEvent) -> None:
        self._refuse("delete")

    def _refuse(self, operation: str) -> None:
        # config rejects a feed as a destination, so reaching this is a wiring bug
        # rather than a user error -- but a silent no-op here would report mirrors
        # as written and let the runner call a doing-nothing pass healthy.
        raise ProviderError(f"cannot {operation}: {self._label} is a read-only ICS feed")

    # -- fetching ------------------------------------------------------------

    def _read(self) -> ICalendar:
        data = retry_call(self._fetch_once)
        try:
            return ICalendar.from_ical(data)
        except (KeyError, ValueError, TypeError) as exc:
            # A feed that answers with a login page or a truncated body answers that
            # way every time; retrying it would burn the pass on every run.
            #
            # ``from None`` because the body is in the exception text and a login
            # page's body contains the feed URL: chained, log.exception() would print
            # the unscrubbed original next to the scrubbed one.
            raise ProviderError(
                f"{self._label} did not return parseable iCalendar: {scrub(exc)}"
            ) from None

    def _fetch_once(self) -> bytes:
        try:
            return self._fetch(self._url)
        except TransientError as exc:
            # Classified by the fetcher, which has no safe way to name the feed.
            raise TransientError(f"{self._label}: {scrub(exc)}", status=exc.status) from None
        except ProviderError as exc:
            raise ProviderError(f"{self._label}: {scrub(exc)}", status=exc.status) from None
        except urllib.error.HTTPError as exc:
            # The status is the whole of what is useful. The reason phrase is not
            # interpolated at all: urllib puts the request URL into it, and on a
            # second hop that URL is the redirect target rather than the configured
            # one -- a string no scrubbing built from the config could anticipate.
            message = f"{self._label} returned HTTP {exc.code}"
            if exc.code in TRANSIENT_STATUSES:
                raise TransientError(message, status=exc.code) from None
            raise ProviderError(message, status=exc.code) from None
        except (OSError, http.client.HTTPException) as exc:
            # URLError (DNS, refused connection, TLS) and TimeoutError are both
            # OSError; a body that stops mid-flight arrives as an HTTPException.
            raise TransientError(f"cannot reach {self._label}: {scrub(exc)}") from None
        except Exception as exc:  # noqa: BLE001 - no unclassified failure may escape
            # Anything unexpected still passed through code holding the URL, so it
            # leaves by the redacting door like everything else.
            raise ProviderError(f"cannot read {self._label}: {scrub(exc)}") from None


def _to_event(component: IEvent) -> CalEvent:
    start, end, all_day = span(component)
    if all_day and end <= start:
        # RFC 5545 3.6.1: DTEND is exclusive, so ``DTSTART;VALUE=DATE:X`` with
        # ``DTEND;VALUE=DATE:X`` names that one day -- feeds do write it. Read
        # literally it is a zero-length all-day event, which reserves nothing.
        #
        # A zero-length *timed* occurrence is left exactly as it is. calsync cannot
        # tell a deliberately zero-length event from one whose DTEND the expander
        # resolved to its DTSTART, and inventing an hour of busy time the feed never
        # asked for writes that hour onto the user's real calendar.
        end = start + dt.timedelta(days=1)
    uid = text(component, "uid")
    description = text(component, "description")
    # The differ keys on (sync id, uid, occurrence start), so the UID a series shares
    # across its occurrences is not a clash; the recurrence id rides along for
    # diagnosis only, and ``raw`` is excluded from equality.
    recurrence_id = component.get("recurrence-id")
    raw = {} if recurrence_id is None else {"recurrence_id": str(getattr(recurrence_id, "dt", ""))}
    return CalEvent(
        uid=uid,
        start=start,
        end=end,
        title=text(component, "summary"),
        description=strip_marker(description),
        location=text(component, "location"),
        all_day=all_day,
        travel_before=travel_before(component, uid),
        cancelled=text(component, "status").upper() == "CANCELLED",
        # A feed carries no identity for whoever is reading it, so no ATTENDEE line
        # on it can be read as the user's own decline.
        declined=False,
        transparent=text(component, "transp").upper() == "TRANSPARENT",
        marker=read_marker(component, uid, description),
        raw=raw,
    )
