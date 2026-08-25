"""Google Calendar provider."""

from __future__ import annotations

from datetime import UTC, datetime, time
from typing import Any

from googleapiclient.errors import HttpError

from calsync.marker import (
    PROP_HASH,
    PROP_KEY,
    PROP_SYNC,
    parse_marker_line,
    stamp_description,
    strip_marker,
)
from calsync.model import CalEvent, Marker
from calsync.providers.base import ProviderError, TransientError, Window
from calsync.retry import retry_call

TRANSIENT_STATUSES = {429, 500, 502, 503, 504}
GONE_STATUSES = {404, 410}
# Google spends 403 on two unrelated things. Quota exhaustion is worth waiting out;
# everything else (no access to the calendar, disabled API, suspended account) is a
# standing condition, and retrying it burns ~14s of backoff on every pass forever.
RATE_LIMIT_REASONS = {"rateLimitExceeded", "userRateLimitExceeded"}
# eventTypes Google puts on a calendar that do not actually block time. Mirroring them
# would add daily noise to the destination, so they are read as free and the default
# skip.free filter drops them. focusTime and outOfOffice are deliberately absent: those
# do block time and must still be mirrored.
FREE_EVENT_TYPES = {"workingLocation", "birthday", "fromGmail"}
UTC_MIDNIGHT = time.min.replace(tzinfo=UTC)
PAGE_SIZE = 250


def _status(error: HttpError) -> int | None:
    return getattr(error.resp, "status", None)


def _is_rate_limited(error: HttpError) -> bool:
    # error_details is "" -- not a list -- when the body carries no error.message,
    # so the type has to be checked before iterating it.
    details = getattr(error, "error_details", None)
    if not isinstance(details, list):
        return False
    return any(
        isinstance(detail, dict) and detail.get("reason") in RATE_LIMIT_REASONS
        for detail in details
    )


def _is_transient(error: HttpError) -> bool:
    status = _status(error)
    if status in TRANSIENT_STATUSES:
        return True
    return status == 403 and _is_rate_limited(error)


def _execute(request: Any) -> Any:
    def call() -> Any:
        try:
            return request.execute()
        except HttpError as exc:
            status = _status(exc)
            if _is_transient(exc):
                raise TransientError(str(exc), status=status) from exc
            raise ProviderError(str(exc), status=status) from exc

    return retry_call(call)


class GoogleProvider:
    """Read and write one Google calendar.

    Two properties of this backend are load-bearing:

    * ``showDeleted=False`` on every list. Google returns cancelled instances of a
      recurring event without ``start``/``end``, so asking for them would push
      unparseable items through ``_to_event``.
    * calsync keeps no state, and the differ compares the hash *stored on a mirror*
      against the hash it recomputes from the source. A user's manual edit to a
      mirror on the destination therefore persists: it changes neither hash, so no
      update is issued and the edit is never repaired.
    """

    def __init__(self, service: Any, calendar_id: str, name: str = "google") -> None:
        self._service = service
        self._calendar_id = calendar_id
        self.name = name

    # -- reading -------------------------------------------------------------

    def list_events(self, window: Window) -> list[CalEvent]:
        return self._query(window)

    def list_mirrors(self, window: Window, sync_id: str) -> list[CalEvent]:
        events = self._query(window, privateExtendedProperty=f"{PROP_SYNC}={sync_id}")
        return [event for event in events if event.is_mirror]

    def _query(self, window: Window, **extra: Any) -> list[CalEvent]:
        start, end = window
        events: list[CalEvent] = []
        page_token: str | None = None
        while True:
            params: dict[str, Any] = {
                "calendarId": self._calendar_id,
                "timeMin": start.isoformat(),
                "timeMax": end.isoformat(),
                "singleEvents": True,
                "orderBy": "startTime",
                "showDeleted": False,
                "maxResults": PAGE_SIZE,
                **extra,
            }
            if page_token:
                params["pageToken"] = page_token
            response = _execute(self._service.events().list(**params))
            events.extend(self._to_event(item) for item in response.get("items", []))
            page_token = response.get("nextPageToken")
            if not page_token:
                return events

    def _to_event(self, item: dict) -> CalEvent:
        # One malformed item must not escape as a bare KeyError/ValueError through the
        # backend-agnostic Provider contract and take the whole sync down with it.
        uid = item.get("id")
        if not uid:
            raise ProviderError("google returned an event with no id")
        try:
            start, all_day = _parse_endpoint(item.get("start") or {})
            end, _ = _parse_endpoint(item.get("end") or {})
        except ProviderError as exc:
            raise ProviderError(f"google event {uid}: {exc}") from exc
        description = item.get("description", "") or ""
        try:
            return CalEvent(
                uid=uid,
                start=start,
                end=end,
                title=item.get("summary", "") or "",
                description=strip_marker(description),
                location=item.get("location", "") or "",
                all_day=all_day,
                cancelled=item.get("status") == "cancelled",
                declined=_self_declined(item),
                transparent=_is_free(item),
                marker=_read_marker(item, description),
                raw=item,
            )
        except ValueError as exc:
            raise ProviderError(f"google event {uid}: {exc}") from exc

    # -- writing -------------------------------------------------------------

    def create(self, event: CalEvent) -> None:
        _execute(self._service.events().insert(calendarId=self._calendar_id, body=_to_body(event)))

    def update(self, event: CalEvent) -> None:
        _execute(
            self._service.events().update(
                calendarId=self._calendar_id, eventId=event.uid, body=_to_body(event)
            )
        )

    def delete(self, event: CalEvent) -> None:
        try:
            _execute(self._service.events().delete(calendarId=self._calendar_id, eventId=event.uid))
        except ProviderError as exc:
            # Already gone is the state we wanted. Anything else -- a transient failure,
            # a permission denial -- has to surface, or a pass that deleted nothing
            # reports itself healthy.
            if exc.status not in GONE_STATUSES:
                raise


def _parse_endpoint(endpoint: dict) -> tuple[datetime, bool]:
    timed = endpoint.get("dateTime")
    day = endpoint.get("date")
    try:
        if timed is not None:
            return datetime.fromisoformat(timed).astimezone(UTC), False
        if day is not None:
            return datetime.combine(datetime.fromisoformat(day).date(), time.min, tzinfo=UTC), True
    except ValueError as exc:
        raise ProviderError(f"unparseable endpoint {endpoint}") from exc
    raise ProviderError("endpoint has neither dateTime nor date")


def _is_free(item: dict) -> bool:
    return item.get("transparency") == "transparent" or item.get("eventType") in FREE_EVENT_TYPES


def _self_declined(item: dict) -> bool:
    for attendee in item.get("attendees", []) or []:
        if attendee.get("self") and attendee.get("responseStatus") == "declined":
            return True
    return False


def _read_marker(item: dict, description: str) -> Marker | None:
    """Identity from the private properties, falling back per field to the marker line.

    The properties are what calsync wrote last, so they win; the description line is
    the fallback for anything that dropped them. It carries the hash as well as the
    key, so a mirror whose properties went missing is still recognised *and* still
    compared -- not rewritten on every pass.
    """
    private = (item.get("extendedProperties") or {}).get("private") or {}
    line = parse_marker_line(description)
    key = private.get(PROP_KEY) or (line[0] if line else "")
    if not key:
        return None
    return Marker(
        sync_id=private.get(PROP_SYNC, ""),
        key=key,
        hash=private.get(PROP_HASH) or (line[1] if line else ""),
    )


def _at_utc_midnight(moment: datetime) -> bool:
    # CalEvent normalises to UTC, so comparing the time of day is a UTC comparison.
    return moment.timetz() == UTC_MIDNIGHT


def _endpoints(event: CalEvent) -> tuple[dict, dict]:
    if event.all_day and _at_utc_midnight(event.start) and _at_utc_midnight(event.end):
        # Google's all-day form. end.date is exclusive, which is exactly how
        # _parse_endpoint reads it back. Written as a timed block at UTC midnight
        # instead, the event renders a day early west of Greenwich and marks the
        # user busy from the previous evening.
        return {"date": event.start.date().isoformat()}, {"date": event.end.date().isoformat()}
    # Padding moved an edge off midnight; the date form cannot express that, and
    # dropping it silently would be worse than keeping the timed block.
    return (
        {"dateTime": event.start.isoformat(), "timeZone": "UTC"},
        {"dateTime": event.end.isoformat(), "timeZone": "UTC"},
    )


def _to_body(event: CalEvent) -> dict:
    marker = event.marker
    if marker is None:
        raise ProviderError("refusing to write an event without a calsync marker")
    start, end = _endpoints(event)
    return {
        "summary": event.title,
        "description": stamp_description(event.description, marker.key, marker.hash),
        "location": event.location,
        "start": start,
        "end": end,
        "transparency": "opaque",
        "reminders": {"useDefault": False},
        "extendedProperties": {
            "private": {
                PROP_SYNC: marker.sync_id,
                PROP_KEY: marker.key,
                PROP_HASH: marker.hash,
            }
        },
    }
