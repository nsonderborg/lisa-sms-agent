"""Google Calendar API client: authentication and event fetching."""
import json
import logging
from datetime import datetime, date, timezone, timedelta

from app.config import (
    GOOGLE_SERVICE_ACCOUNT_JSON,
    GOOGLE_CLIENT_ID,
    GOOGLE_CLIENT_SECRET,
    GOOGLE_REFRESH_TOKEN,
)

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/calendar"]
# Read-write on events (narrowest scope for CRUD).
# Supports two auth methods (checked in order):
#   1. Service account JSON  — set GOOGLE_SERVICE_ACCOUNT_JSON env var
#   2. OAuth2 refresh token  — set GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REFRESH_TOKEN


def build_calendar_service():
    """Build Google Calendar API v3 service.

    Tries service account auth first, then OAuth2 refresh token.
    Raises ValueError if neither set of credentials is available.
    """
    from googleapiclient.discovery import build

    if GOOGLE_SERVICE_ACCOUNT_JSON.strip():
        from google.oauth2 import service_account
        info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
        creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    elif GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET and GOOGLE_REFRESH_TOKEN:
        from google.oauth2.credentials import Credentials
        creds = Credentials(
            token=None,
            refresh_token=GOOGLE_REFRESH_TOKEN,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=GOOGLE_CLIENT_ID,
            client_secret=GOOGLE_CLIENT_SECRET,
            scopes=SCOPES,
        )
    else:
        raise ValueError(
            "No Google Calendar credentials found. "
            "Set GOOGLE_SERVICE_ACCOUNT_JSON, or set "
            "GOOGLE_CLIENT_ID + GOOGLE_CLIENT_SECRET + GOOGLE_REFRESH_TOKEN."
        )
    return build("calendar", "v3", credentials=creds)


def fetch_events_for_window(service, calendar_id: str, days_ahead: int = 30) -> list[dict]:
    """Fetch all events from now to days_ahead days in the future.

    Always uses singleEvents=True to expand recurring events.
    Handles pagination via nextPageToken.
    """
    now = datetime.now(timezone.utc)
    time_min = now.isoformat()
    time_max = (now + timedelta(days=days_ahead)).isoformat()

    events = []
    page_token = None
    while True:
        result = service.events().list(
            calendarId=calendar_id,
            timeMin=time_min,
            timeMax=time_max,
            singleEvents=True,
            orderBy="startTime",
            pageToken=page_token,
            maxResults=2500,
        ).execute()
        events.extend(result.get("items", []))
        page_token = result.get("nextPageToken")
        if not page_token:
            break
    return events


def normalise_event_times(event: dict) -> tuple[datetime, datetime] | None:
    """Extract UTC start/end from a Calendar API event dict.

    Handles both timed events (dateTime field) and all-day events (date field).
    Returns None if event lacks parseable times.
    """
    try:
        raw_start = event["start"].get("dateTime") or event["start"].get("date")
        raw_end = event["end"].get("dateTime") or event["end"].get("date")
        if not raw_start or not raw_end:
            return None
        start = _parse_google_datetime(raw_start)
        end = _parse_google_datetime(raw_end)
        return start, end
    except (KeyError, ValueError):
        return None


def create_event(
    service,
    calendar_id: str,
    summary: str,
    start_dt: datetime,
    end_dt: datetime,
    description: str = "",
) -> dict:
    """Create a Google Calendar event.

    Args:
        service: Google Calendar API service object (from build_calendar_service).
        calendar_id: Calendar ID (e.g. "primary" or a specific calendar email).
        summary: Event title (e.g. "Koretime - Hans Jensen").
        start_dt: Event start as a timezone-aware datetime.
        end_dt: Event end as a timezone-aware datetime.
        description: Optional event description.

    Returns:
        The full event resource dict from Google, including 'id'.
    """
    event_body = {
        "summary": summary,
        "description": description,
        "start": {"dateTime": start_dt.isoformat(), "timeZone": "Europe/Copenhagen"},
        "end": {"dateTime": end_dt.isoformat(), "timeZone": "Europe/Copenhagen"},
    }
    return service.events().insert(calendarId=calendar_id, body=event_body).execute()



def delete_event(service, calendar_id: str, event_id: str) -> None:
    """Delete a Google Calendar event.

    Args:
        service: Google Calendar API service object.
        calendar_id: Calendar ID.
        event_id: The event ID to delete.

    Returns:
        None (Google API returns empty body on successful delete).
    """
    service.events().delete(calendarId=calendar_id, eventId=event_id).execute()


def _parse_google_datetime(raw: str) -> datetime:
    """Parse RFC3339 dateTime or yyyy-mm-dd date into a UTC-aware datetime."""
    if "T" in raw:
        dt = datetime.fromisoformat(raw)
        return dt.astimezone(timezone.utc)
    else:
        d = date.fromisoformat(raw)
        return datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
