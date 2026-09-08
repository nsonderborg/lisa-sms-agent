import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx
from icalendar import Calendar

from app.config import CALENDAR_PHONE_PATTERN, ICAL_URL

logger = logging.getLogger(__name__)

_phone_re = re.compile(CALENDAR_PHONE_PATTERN)


@dataclass
class CalendarEvent:
    summary: str
    start: datetime
    phone_number: str | None
    location: str | None
    uid: str


def fetch_events() -> list[CalendarEvent]:
    """Fetch and parse events from the iCal subscription URL."""
    if not ICAL_URL:
        logger.debug("No ICAL_URL configured, skipping calendar poll")
        return []

    try:
        response = httpx.get(ICAL_URL, timeout=30, follow_redirects=True)
        response.raise_for_status()
    except Exception:
        logger.exception("Failed to fetch iCal feed from %s", ICAL_URL)
        return []

    return parse_ics(response.text)


def parse_ics(ics_text: str) -> list[CalendarEvent]:
    """Parse an .ics string into CalendarEvent objects."""
    try:
        cal = Calendar.from_ical(ics_text)
    except Exception:
        logger.exception("Failed to parse iCal data")
        return []

    events = []
    for component in cal.walk():
        if component.name != "VEVENT":
            continue

        summary = str(component.get("SUMMARY", ""))
        dtstart = component.get("DTSTART")
        uid = str(component.get("UID", ""))
        location = str(component.get("LOCATION", "")) or None

        if not dtstart:
            continue

        start = dtstart.dt
        # Convert date to datetime if needed
        if not isinstance(start, datetime):
            start = datetime.combine(start, datetime.min.time(), tzinfo=timezone.utc)
        # Ensure timezone-aware
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)

        # Extract phone number from summary or description
        phone = _extract_phone(summary)
        if not phone:
            description = str(component.get("DESCRIPTION", ""))
            phone = _extract_phone(description)

        events.append(
            CalendarEvent(
                summary=summary,
                start=start,
                phone_number=phone,
                location=location,
                uid=uid,
            )
        )

    logger.info("Parsed %d events from iCal feed", len(events))
    return events


def _extract_phone(text: str) -> str | None:
    """Extract a Danish phone number from text using the configured pattern."""
    match = _phone_re.search(text)
    return match.group(0) if match else None
