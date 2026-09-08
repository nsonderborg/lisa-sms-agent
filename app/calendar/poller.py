"""Calendar poll job: fetch events and cache locally.

Backend-agnostic — uses whichever calendar backend is selected by the
``calendar_backend`` setting in ``config.yaml`` (``google`` or ``caldav``).
"""
import logging
import threading
from datetime import datetime, timedelta, timezone

from app.calendar.client import (
    build_calendar_service,
    fetch_busy_intervals_all_calendars,
    fetch_events_for_window,
    normalise_event_times,
)
from app.config import (
    CALENDAR_BACKEND,
    CALENDAR_CACHE_DAYS_AHEAD,
    GCAL_CALENDAR_ID,
)
from app.database import SessionLocal
from app.models import CalendarCache

logger = logging.getLogger(__name__)


def poll_calendar() -> int:
    """Fetch events from the configured calendar backend and cache locally.

    Returns the number of events cached, or 0 if credentials are missing or
    the API call fails.
    """
    if CALENDAR_BACKEND == "caldav":
        # Phase 999.30: privacy-preserving multi-calendar busy-read. Reads the
        # owner busy across ALL calendars and caches only content-free busy
        # intervals (summary="") so Lisa never double-books, while learning
        # nothing about what/where/with-whom. Writes stay on the Lisa calendar.
        try:
            intervals = fetch_busy_intervals_all_calendars(CALENDAR_CACHE_DAYS_AHEAD)
        except ValueError as e:
            logger.warning("Calendar credentials not ready (%s), skipping poll", e)
            return 0
        except Exception:
            # Per-calendar content-bearing failures are already swallowed inside
            # fetch_busy_intervals_all_calendars; this traceback is our own code.
            logger.exception("Failed to fetch busy intervals from caldav calendars")
            return 0

        session = SessionLocal()
        try:
            session.query(CalendarCache).delete()
            count = 0
            for i, (start_utc, end_utc) in enumerate(intervals):
                session.add(
                    CalendarCache(
                        # Cache is fully cleared each poll, so the loop index is
                        # a unique event_id within the batch.
                        event_id=f"busy-{i}",
                        summary="",  # privacy: content-free by construction
                        start_dt=start_utc.replace(tzinfo=None),  # naive UTC
                        end_dt=end_utc.replace(tzinfo=None),
                        fetched_at=datetime.now(timezone.utc).replace(tzinfo=None),
                    )
                )
                count += 1
            session.commit()
            logger.info("Cached %d busy intervals from caldav calendars", count)
            return count
        except Exception:
            session.rollback()
            logger.exception("Failed to write busy intervals to CalendarCache")
            return 0
        finally:
            session.close()

    try:
        service = build_calendar_service()
    except ValueError as e:
        logger.warning("Calendar credentials not ready (%s), skipping poll", e)
        return 0

    try:
        # GCAL_CALENDAR_ID is ignored by the CalDAV backend (service is
        # already a Calendar handle there) — kept for the Google path.
        raw_events = fetch_events_for_window(
            service, GCAL_CALENDAR_ID, CALENDAR_CACHE_DAYS_AHEAD
        )
    except Exception:
        logger.exception("Failed to fetch events from %s calendar", CALENDAR_BACKEND)
        return 0

    session = SessionLocal()
    try:
        # Clear existing cache and replace with fresh data
        session.query(CalendarCache).delete()

        count = 0
        for raw_event in raw_events:
            # Skip free/transparent events (auto-generated holidays, birthday reminders, etc.)
            # Only opaque (busy) events block slots. Default when field absent is opaque.
            if raw_event.get("transparency") == "transparent":
                continue
            times = normalise_event_times(raw_event)
            if times is None:
                continue
            start_dt, end_dt = times
            entry = CalendarCache(
                event_id=raw_event.get("id", f"unknown-{count}"),
                # Privacy: store NO event content. Only the busy interval (start/end)
                # is used for slot computation; the title/summary is never read by
                # the booking logic, so we deliberately do not persist it. Keeps the
                # owner's calendar content out of Lisa's DB entirely.
                summary="",
                start_dt=start_dt.replace(tzinfo=None),  # store as naive UTC
                end_dt=end_dt.replace(tzinfo=None),
                fetched_at=datetime.now(timezone.utc).replace(tzinfo=None),
            )
            session.add(entry)
            count += 1

        session.commit()
        logger.info("Cached %d events from %s calendar", count, CALENDAR_BACKEND)
        return count
    except Exception:
        session.rollback()
        logger.exception("Failed to write events to CalendarCache")
        return 0
    finally:
        session.close()


# In-memory poll-attempt marker + guard. Tracks the last time we KICKED a
# refresh, decoupled from cached-row count so an empty calendar (0 events ->
# 0 cache rows) doesn't look "never polled" and retrigger on every message.
# Single-instance deployment, so module-level state is sufficient.
_poll_lock = threading.Lock()
_last_poll_attempt: datetime | None = None
_poll_in_progress: bool = False


def _run_poll_in_background() -> None:
    """Run poll_calendar() off the request thread, clearing the guard after."""
    global _poll_in_progress
    try:
        poll_calendar()
    finally:
        with _poll_lock:
            _poll_in_progress = False


def refresh_if_stale(max_age_minutes: int = 15) -> int:
    """Trigger a calendar refresh if the cache is stale — NON-BLOCKING.

    Called before slot searches. It must NEVER make a CalDAV/GCal call on the
    calling (webhook) thread: a slow backend pushes the webhook past Twilio's
    ~15s timeout, which returns HTTP 502 to Twilio and silently drops the SMS
    reply (observed in production 2026-05-29 — student got no response). So a
    stale cache kicks the poll onto a daemon thread and we serve the current
    cache immediately; the background scheduler also polls on its own interval.

    Staleness uses the newest cached row's timestamp, falling back to an
    in-memory last-attempt marker so a legitimately empty calendar isn't
    treated as "never polled" and re-polled on every single message.

    Returns 0 always (kept for signature compatibility — callers ignore it and
    read from the cache directly).
    """
    global _last_poll_attempt, _poll_in_progress

    now = datetime.now(timezone.utc).replace(tzinfo=None)

    session = SessionLocal()
    try:
        from sqlalchemy import func
        latest = session.query(func.max(CalendarCache.fetched_at)).scalar()
    finally:
        session.close()

    reference = latest or _last_poll_attempt
    if reference is not None and (now - reference) <= timedelta(minutes=max_age_minutes):
        return 0  # fresh enough — serve cache

    # Stale (or never polled) — refresh in the background, don't block the request.
    with _poll_lock:
        if _poll_in_progress:
            return 0
        _poll_in_progress = True
        _last_poll_attempt = now

    logger.info("Calendar cache stale — refreshing in background")
    threading.Thread(
        target=_run_poll_in_background, name="calendar-poll", daemon=True
    ).start()
    return 0
