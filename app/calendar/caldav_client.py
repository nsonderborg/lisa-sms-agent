"""CalDAV calendar client.

Implements the same public surface as ``gcal_client`` so the booking and
cancellation handlers can be backend-agnostic via ``app.calendar.client``.

Targeted at iCloud CalDAV (default URL ``https://caldav.icloud.com/``) but
works with any RFC 4791 server. Authentication is HTTP Basic with an
app-specific password generated at appleid.apple.com.

The "service" object returned by :func:`build_calendar_service` is a
:class:`caldav.Calendar` handle for the calendar named by
``CALDAV_CALENDAR_NAME`` (default ``"Lisa"``). The ``calendar_id``
parameter on subsequent functions is ignored and kept only for signature
compatibility with the Google backend.
"""
import logging
import uuid
from datetime import date as _date
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)


class CalendarSafetyError(RuntimeError):
    """A delete was refused because its target lies outside the Lisa calendar.

    Never caught to "retry without the guard" — it means the URL Lisa was about
    to delete does not belong to the collection Lisa owns.
    """


def _lisa_collection_url(service) -> str:
    """The Lisa calendar's collection URL, normalised to exactly one trailing slash.

    ``service`` IS the Lisa calendar (see the module docstring), so its URL is the
    collection every event Lisa creates lives under.
    """
    return str(getattr(service, "url", "")).rstrip("/") + "/"


def build_calendar_service():
    """Connect to CalDAV and return the named calendar.

    Raises ``ValueError`` with a clear message if credentials are missing
    or the configured calendar does not exist on the account.
    """
    from app.config import (
        CALDAV_CALENDAR_NAME,
        CALDAV_PASSWORD,
        CALDAV_URL,
        CALDAV_USERNAME,
    )
    import caldav

    if not (CALDAV_URL and CALDAV_USERNAME and CALDAV_PASSWORD):
        raise ValueError(
            "CalDAV credentials missing: set CALDAV_URL, CALDAV_USERNAME, "
            "CALDAV_PASSWORD."
        )

    client = caldav.DAVClient(
        url=CALDAV_URL,
        username=CALDAV_USERNAME,
        password=CALDAV_PASSWORD,
    )
    principal = client.principal()
    calendars = principal.calendars()
    target = (CALDAV_CALENDAR_NAME or "Lisa").strip()
    for cal in calendars:
        if str(cal.get_display_name()).strip() == target:
            return cal

    available = ", ".join(sorted(str(c.get_display_name()) for c in calendars)) or "<none>"
    raise ValueError(
        f"CalDAV calendar named {target!r} not found on this account. "
        f"Available calendars: {available}. Create the calendar in Apple "
        f"Calendar first, or update caldav_calendar_name in config.yaml."
    )


def create_event(
    service,
    calendar_id: str,  # ignored — service IS the calendar for CalDAV
    summary: str,
    start_dt: datetime,
    end_dt: datetime,
    description: str = "",
) -> dict:
    """Create an event on the configured CalDAV calendar.

    Returns ``{"id": <event url>}``. iCloud rejects the calendar-query REPORT
    used by ``Calendar.event_by_uid()`` with 412 Precondition Failed, so we
    keep the server-assigned URL from ``save_event`` and use it directly in
    :func:`delete_event`. The URL fits comfortably in
    ``Booking.gcal_event_id`` (String(200); iCloud URLs are ~120 chars).
    """
    from icalendar import Calendar as ICal
    from icalendar import Event as IEvent

    uid = f"{uuid.uuid4()}@lisa"
    cal = ICal()
    cal.add("prodid", "-//Lisa Driving School Secretary//EN")
    cal.add("version", "2.0")

    ev = IEvent()
    ev.add("uid", uid)
    ev.add("summary", summary)
    if description:
        ev.add("description", description)
    ev.add("dtstart", start_dt)
    ev.add("dtend", end_dt)
    ev.add("dtstamp", datetime.now(timezone.utc))
    cal.add_component(ev)

    ical_text = cal.to_ical().decode("utf-8")
    saved = service.save_event(ical_text)
    return {"id": str(saved.url)}


def delete_event(service, calendar_id: str, event_id: str) -> None:
    """Delete the event by its server-assigned URL (returned at create time).

    Refuses any URL outside the Lisa collection. The credentials Lisa uses own the
    owner's *personal* calendars too, and a CalDAV delete is immediate and
    unrecoverable — there is no trash to restore from. Deleting a real appointment
    out of an instructor's own calendar would end the pilot, so the "Lisa calendar
    only" rule is enforced here, at the single point that can actually delete,
    rather than left to hold at every present and future call site.

    Today every caller passes ``Booking.gcal_event_id``, which only ever receives
    URLs from :func:`create_event` — but that safety is emergent, not enforced. A
    manual DB edit, a corrupted row, or a future path that stores a foreign event
    URL would otherwise delete from a personal calendar with no resistance.

    Raises :class:`CalendarSafetyError` on a foreign URL. Callers treat the delete
    as best-effort and log, so refusing leaves a stale event behind — visible and
    fixable, unlike the alternative.
    """
    import caldav

    collection = _lisa_collection_url(service)
    if len(collection) <= 1 or not str(event_id).startswith(collection):
        raise CalendarSafetyError(
            f"refusing to delete {event_id!r}: outside the Lisa collection "
            f"({collection!r}). Lisa may only ever delete events it created."
        )

    event = caldav.Event(parent=service, url=event_id)
    event.delete()


def fetch_events_for_window(
    service, calendar_id: str, days_ahead: int = 30
) -> list[dict]:
    """Fetch events from now to ``days_ahead`` days out.

    Returns Google-Calendar-shaped dicts (``id``, ``summary``, ``start``,
    ``end``, ``transparency``) so :func:`normalise_event_times` and the
    poller can consume both backends with the same code path.
    """
    now = datetime.now(timezone.utc)
    end = now + timedelta(days=days_ahead)
    raw = service.search(start=now, end=end, event=True, expand=True)

    out: list[dict] = []
    for r in raw:
        ical = getattr(r, "icalendar_instance", None)
        if ical is None:
            continue
        for component in ical.walk("VEVENT"):
            uid = str(component.get("uid") or r.url)
            summary = str(component.get("summary") or "")
            dtstart = component.get("dtstart")
            dtend = component.get("dtend")
            if dtstart is None or dtend is None:
                continue
            transp = str(component.get("transp") or "OPAQUE").upper()
            out.append(
                {
                    "id": uid,
                    "summary": summary,
                    "start": _format_caldav_time(dtstart.dt),
                    "end": _format_caldav_time(dtend.dt),
                    "transparency": "transparent" if transp == "TRANSPARENT" else "opaque",
                }
            )
    return out


def _extract_busy_intervals(raw_results) -> list[tuple[datetime, datetime]]:
    """Extract ONLY (start, end) UTC tuples from search results.

    Content-free by construction: never reads SUMMARY/LOCATION/etc, never
    builds a dict — the return type cannot carry event content downstream.
    Skips all-day events (FB-04) and TRANSP=TRANSPARENT events (FB-05);
    absent TRANSP defaults to busy/OPAQUE.
    """
    out: list[tuple[datetime, datetime]] = []
    for r in raw_results:
        ical = getattr(r, "icalendar_instance", None)
        if ical is None:
            continue
        for component in ical.walk("VEVENT"):
            dtstart = component.get("dtstart")
            dtend = component.get("dtend")
            if dtstart is None or dtend is None:
                continue
            # FB-04: all-day events are DATE-valued -> a `date`, not `datetime`.
            # (datetime subclasses date, so check datetime first.)
            if not isinstance(dtstart.dt, datetime):
                continue
            # FB-05: transparent events are free; absent TRANSP defaults busy.
            transp = str(component.get("transp") or "OPAQUE").upper()
            if transp == "TRANSPARENT":
                continue
            start = dtstart.dt if dtstart.dt.tzinfo else dtstart.dt.replace(tzinfo=timezone.utc)
            end = dtend.dt if dtend.dt.tzinfo else dtend.dt.replace(tzinfo=timezone.utc)
            out.append((start.astimezone(timezone.utc), end.astimezone(timezone.utc)))
    return out


def fetch_busy_intervals_all_calendars(days_ahead: int = 30) -> list[tuple[datetime, datetime]]:
    """Read busy (start, end) intervals across ALL the owner's calendars.

    Privacy-preserving: enumerates every calendar on the principal (FB-01)
    and returns only content-free intervals (FB-06). iCloud does not support
    VFREEBUSY, so this enumerate-and-strip path is the sole strategy (FB-07).
    WRITES are unaffected — they stay on the single Lisa calendar.
    """
    from app.config import CALDAV_PASSWORD, CALDAV_URL, CALDAV_USERNAME
    import caldav

    if not (CALDAV_URL and CALDAV_USERNAME and CALDAV_PASSWORD):
        raise ValueError(
            "CalDAV credentials missing: set CALDAV_URL, CALDAV_USERNAME, "
            "CALDAV_PASSWORD."
        )

    client = caldav.DAVClient(url=CALDAV_URL, username=CALDAV_USERNAME, password=CALDAV_PASSWORD)
    principal = client.principal()
    calendars = principal.calendars()

    now = datetime.now(timezone.utc)
    end = now + timedelta(days=days_ahead)

    all_busy: list[tuple[datetime, datetime]] = []
    for cal in calendars:
        try:
            raw = cal.search(start=now, end=end, event=True, expand=True)
        except Exception as e:
            # Log display-name + exception type/message only — never the raw
            # iCal body (could carry other calendars' content). No traceback dump.
            logger.warning(
                "Skipping calendar %s during busy-read: %s: %s",
                cal.get_display_name(), type(e).__name__, e,
            )
            continue
        all_busy.extend(_extract_busy_intervals(raw))
    return all_busy


def normalise_event_times(event: dict) -> tuple[datetime, datetime] | None:
    """Mirror of :func:`gcal_client.normalise_event_times` for CalDAV dicts."""
    try:
        raw_start = event["start"].get("dateTime") or event["start"].get("date")
        raw_end = event["end"].get("dateTime") or event["end"].get("date")
        if not raw_start or not raw_end:
            return None
        start = _parse_iso_datetime(raw_start)
        end = _parse_iso_datetime(raw_end)
        return start, end
    except (KeyError, ValueError):
        return None


def _format_caldav_time(dt) -> dict:
    if isinstance(dt, datetime):
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return {"dateTime": dt.isoformat()}
    if isinstance(dt, _date):
        return {"date": dt.isoformat()}
    return {"dateTime": str(dt)}


def _parse_iso_datetime(raw: str) -> datetime:
    if "T" in raw:
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    d = _date.fromisoformat(raw)
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
