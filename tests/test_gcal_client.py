"""Tests for Google Calendar API client: auth and event fetching."""
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from app.calendar.gcal_client import (
    build_calendar_service,
    fetch_events_for_window,
    normalise_event_times,
    create_event,
    delete_event,
)


# ---------------------------------------------------------------------------
# Authentication tests
# ---------------------------------------------------------------------------


def test_build_service_from_env():
    """build_calendar_service() returns a service object given valid JSON env var."""
    fake_json = '{"type": "service_account", "project_id": "test"}'

    with patch("app.calendar.gcal_client.GOOGLE_SERVICE_ACCOUNT_JSON", fake_json):
        with patch("google.oauth2.service_account.Credentials.from_service_account_info") as mock_creds:
            with patch("googleapiclient.discovery.build") as mock_build:
                mock_creds.return_value = MagicMock()
                mock_build.return_value = MagicMock()

                service = build_calendar_service()

                mock_creds.assert_called_once()
                mock_build.assert_called_once_with(
                    "calendar", "v3", credentials=mock_creds.return_value
                )
                assert service is mock_build.return_value


def test_missing_env_var():
    """build_calendar_service() raises ValueError when NO credential set is configured.

    Both auth methods must be blanked: with only the service-account JSON empty,
    the function falls through to the OAuth2 path — and OAuth2 vars leaking in
    from a local .env would make this test pass/fail depending on the machine.
    """
    with patch("app.calendar.gcal_client.GOOGLE_SERVICE_ACCOUNT_JSON", ""), \
         patch("app.calendar.gcal_client.GOOGLE_CLIENT_ID", ""), \
         patch("app.calendar.gcal_client.GOOGLE_CLIENT_SECRET", ""), \
         patch("app.calendar.gcal_client.GOOGLE_REFRESH_TOKEN", ""):
        with pytest.raises(ValueError) as exc_info:
            build_calendar_service()

    assert "GOOGLE_SERVICE_ACCOUNT_JSON" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Event fetching tests
# ---------------------------------------------------------------------------


def _make_service(pages: list[dict]) -> MagicMock:
    """Build a mock service that returns pages sequentially."""
    service = MagicMock()
    # Each call to .execute() returns the next page dict
    service.events().list().execute.side_effect = pages
    return service


def test_fetch_events_single_page():
    """fetch_events_for_window() returns items from a single-page response."""
    page = {"items": [{"id": "event-1"}, {"id": "event-2"}]}
    service = _make_service([page])

    result = fetch_events_for_window(service, "cal@example.com", days_ahead=7)

    assert result == [{"id": "event-1"}, {"id": "event-2"}]


def test_fetch_events_pagination():
    """fetch_events_for_window() follows nextPageToken to collect all pages."""
    page1 = {"items": [{"id": "event-1"}], "nextPageToken": "tok-abc"}
    page2 = {"items": [{"id": "event-2"}, {"id": "event-3"}]}

    service = _make_service([page1, page2])

    result = fetch_events_for_window(service, "cal@example.com", days_ahead=7)

    assert len(result) == 3
    assert result[0] == {"id": "event-1"}
    assert result[2] == {"id": "event-3"}


def test_fetch_events_empty():
    """fetch_events_for_window() returns empty list when no events."""
    page = {"items": []}
    service = _make_service([page])

    result = fetch_events_for_window(service, "cal@example.com", days_ahead=7)

    assert result == []


def test_fetch_events_uses_single_events_and_order():
    """fetch_events_for_window() passes singleEvents=True and orderBy='startTime'."""
    page = {"items": []}
    service = MagicMock()
    service.events().list().execute.return_value = page

    fetch_events_for_window(service, "cal@example.com", days_ahead=7)

    call_kwargs = service.events().list.call_args.kwargs
    assert call_kwargs["singleEvents"] is True
    assert call_kwargs["orderBy"] == "startTime"


# ---------------------------------------------------------------------------
# normalise_event_times tests
# ---------------------------------------------------------------------------


def test_normalise_timed_event():
    """normalise_event_times() converts dateTime fields to UTC datetimes."""
    event = {
        "start": {"dateTime": "2026-04-15T10:00:00+02:00"},
        "end": {"dateTime": "2026-04-15T11:30:00+02:00"},
    }
    result = normalise_event_times(event)

    assert result is not None
    start, end = result
    assert start == datetime(2026, 4, 15, 8, 0, tzinfo=timezone.utc)
    assert end == datetime(2026, 4, 15, 9, 30, tzinfo=timezone.utc)


def test_normalise_allday_event():
    """normalise_event_times() converts date fields to midnight UTC datetimes."""
    event = {
        "start": {"date": "2026-04-15"},
        "end": {"date": "2026-04-16"},
    }
    result = normalise_event_times(event)

    assert result is not None
    start, end = result
    assert start == datetime(2026, 4, 15, 0, 0, tzinfo=timezone.utc)
    assert end == datetime(2026, 4, 16, 0, 0, tzinfo=timezone.utc)


def test_normalise_missing_keys():
    """normalise_event_times() returns None when start/end keys are missing."""
    event = {"start": {}, "end": {}}
    result = normalise_event_times(event)
    assert result is None


def test_normalise_missing_start_key():
    """normalise_event_times() returns None when event dict has no start key at all."""
    event = {}
    result = normalise_event_times(event)
    assert result is None


def test_normalise_utc_timed_event():
    """normalise_event_times() handles events already in UTC (Z suffix)."""
    event = {
        "start": {"dateTime": "2026-04-15T08:00:00+00:00"},
        "end": {"dateTime": "2026-04-15T09:30:00+00:00"},
    }
    result = normalise_event_times(event)

    assert result is not None
    start, end = result
    assert start == datetime(2026, 4, 15, 8, 0, tzinfo=timezone.utc)
    assert end == datetime(2026, 4, 15, 9, 30, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Scope verification
# ---------------------------------------------------------------------------


def test_scopes_include_write_access():
    """SCOPES must grant write access for event creation (D-07).

    The full `auth/calendar` scope includes event write access. The real
    regression this pins is someone narrowing SCOPES to a read-only scope,
    which would silently break create_event/delete_event.
    """
    from app.calendar.gcal_client import SCOPES
    write_scopes = {
        "https://www.googleapis.com/auth/calendar",
        "https://www.googleapis.com/auth/calendar.events",
    }
    assert write_scopes & set(SCOPES), f"No write-capable scope in {SCOPES}"
    assert not any("readonly" in s for s in SCOPES)


# ---------------------------------------------------------------------------
# Calendar write function tests
# ---------------------------------------------------------------------------


def test_create_event_calls_insert():
    """create_event() calls events().insert() with correct calendarId and body (CAL-05)."""
    fake_event = {"id": "evt-abc123", "summary": "Koretime - Hans"}
    service = MagicMock()
    service.events().insert().execute.return_value = fake_event

    result = create_event(
        service,
        calendar_id="cal@example.com",
        summary="Koretime - Hans",
        start_dt=datetime(2026, 4, 20, 10, 0, tzinfo=timezone.utc),
        end_dt=datetime(2026, 4, 20, 11, 30, tzinfo=timezone.utc),
        description="Driving lesson",
    )

    assert result == fake_event
    assert result["id"] == "evt-abc123"
    call_kwargs = service.events().insert.call_args.kwargs
    assert call_kwargs["calendarId"] == "cal@example.com"
    body = call_kwargs["body"]
    assert body["summary"] == "Koretime - Hans"
    assert body["description"] == "Driving lesson"
    assert body["start"]["timeZone"] == "Europe/Copenhagen"
    assert body["end"]["timeZone"] == "Europe/Copenhagen"


def test_create_event_default_description():
    """create_event() with no description still includes empty string in body."""
    service = MagicMock()
    service.events().insert().execute.return_value = {"id": "evt-1"}

    create_event(
        service,
        calendar_id="cal@example.com",
        summary="Test",
        start_dt=datetime(2026, 4, 20, 10, 0, tzinfo=timezone.utc),
        end_dt=datetime(2026, 4, 20, 11, 0, tzinfo=timezone.utc),
    )

    body = service.events().insert.call_args.kwargs["body"]
    assert "description" in body
    assert body["description"] == ""



def test_delete_event_calls_delete():
    """delete_event() calls events().delete() with correct IDs and returns None (CAL-06)."""
    service = MagicMock()
    service.events().delete().execute.return_value = None

    result = delete_event(service, calendar_id="cal@example.com", event_id="evt-abc123")

    assert result is None
    call_kwargs = service.events().delete.call_args.kwargs
    assert call_kwargs["calendarId"] == "cal@example.com"
    assert call_kwargs["eventId"] == "evt-abc123"
