"""Tests for app/calendar/poller.py — backend-agnostic calendar poll job."""
from datetime import datetime, timezone
from unittest.mock import MagicMock


from app.models import CalendarCache


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_raw_event(event_id: str, summary: str = "Lesson", hours_offset: int = 1) -> dict:
    """Build a minimal Google Calendar API event dict."""
    base = datetime(2026, 6, 1, 10, 0, 0, tzinfo=timezone.utc)
    from datetime import timedelta
    start = base + timedelta(hours=hours_offset)
    end = start + timedelta(hours=1)
    return {
        "id": event_id,
        "summary": summary,
        "start": {"dateTime": start.isoformat()},
        "end": {"dateTime": end.isoformat()},
    }


def _make_unparseable_event() -> dict:
    """Build an event with missing start/end — normalise_event_times returns None."""
    return {"id": "bad-event", "summary": "Missing times", "start": {}, "end": {}}


# ---------------------------------------------------------------------------
# test_poll_writes_to_cache
# ---------------------------------------------------------------------------

def test_refresh_if_stale_is_nonblocking(db_session, mocker):
    """refresh_if_stale must NOT call poll_calendar on the request thread.

    Regression for the 2026-05-29 production 502: a synchronous CalDAV poll
    inside the webhook blew past Twilio's timeout and dropped the reply. The
    poll must be deferred to a background thread; the call returns immediately.
    """
    import app.calendar.poller as poller

    # Reset module-level guards so the test is order-independent.
    poller._last_poll_attempt = None
    poller._poll_in_progress = False

    mocker.patch("app.calendar.poller.SessionLocal", return_value=db_session)  # empty cache -> None
    mock_poll = mocker.patch("app.calendar.poller.poll_calendar")

    started = {}

    class _FakeThread:
        def __init__(self, target=None, **kwargs):
            started["target"] = target

        def start(self):
            started["started"] = True  # deliberately does NOT run target

    mocker.patch("app.calendar.poller.threading.Thread", _FakeThread)

    result = poller.refresh_if_stale()

    # Returned immediately, deferred the poll to a thread, never ran it inline.
    assert result == 0
    assert started.get("started") is True
    assert started["target"] is poller._run_poll_in_background
    mock_poll.assert_not_called()

    # Cleanup
    poller._last_poll_attempt = None
    poller._poll_in_progress = False


def test_poll_writes_to_cache(db_session, mocker):
    """Successful poll on the google backend writes event rows to CalendarCache."""
    raw_events = [_make_raw_event("evt-1"), _make_raw_event("evt-2", hours_offset=2)]

    mock_service = MagicMock()
    mocker.patch("app.calendar.poller.CALENDAR_BACKEND", "google")
    mocker.patch("app.calendar.poller.build_calendar_service", return_value=mock_service)
    mocker.patch("app.calendar.poller.fetch_events_for_window", return_value=raw_events)
    mocker.patch("app.calendar.poller.GCAL_CALENDAR_ID", "test@group.calendar.google.com")
    mocker.patch("app.calendar.poller.SessionLocal", return_value=db_session)

    from app.calendar.poller import poll_calendar

    result = poll_calendar()

    assert result == 2
    assert db_session.query(CalendarCache).count() == 2


# NOTE: test_poll_skips_if_no_calendar_id was removed (2026-07-03). The runtime
# guard it asserted no longer exists — an empty gcal_calendar_id is now rejected
# at startup by validate_config() ("required when calendar_backend=google"),
# covered by tests/test_config_validation.py::test_empty_gcal_calendar_id_produces_error.


# ---------------------------------------------------------------------------
# test_poll_skips_if_no_credentials
# ---------------------------------------------------------------------------

def test_poll_skips_if_no_credentials(mocker):
    """Returns 0 when no Google credentials are configured.

    Credential checking lives in build_calendar_service() (raises ValueError);
    poll_calendar() must catch it and skip the poll without crashing.
    """
    mocker.patch("app.calendar.poller.CALENDAR_BACKEND", "google")
    mocker.patch(
        "app.calendar.poller.build_calendar_service",
        side_effect=ValueError("No Google Calendar credentials found."),
    )
    mock_fetch = mocker.patch("app.calendar.poller.fetch_events_for_window")

    from app.calendar.poller import poll_calendar

    result = poll_calendar()

    assert result == 0
    mock_fetch.assert_not_called()


# ---------------------------------------------------------------------------
# test_poll_handles_api_error
# ---------------------------------------------------------------------------

def test_poll_handles_api_error(mocker):
    """Returns 0 and does not crash when the Google Calendar API call fails."""
    mocker.patch("app.calendar.poller.CALENDAR_BACKEND", "google")
    mocker.patch("app.calendar.poller.build_calendar_service", return_value=MagicMock())
    mocker.patch(
        "app.calendar.poller.fetch_events_for_window",
        side_effect=Exception("API down"),
    )

    from app.calendar.poller import poll_calendar

    result = poll_calendar()

    assert result == 0  # no crash


# ---------------------------------------------------------------------------
# test_poll_skips_unparseable_events
# ---------------------------------------------------------------------------

def test_poll_skips_unparseable_events(db_session, mocker):
    """Events where normalise_event_times returns None are silently skipped."""
    raw_events = [_make_raw_event("evt-good"), _make_unparseable_event()]

    mock_service = MagicMock()
    mocker.patch("app.calendar.poller.CALENDAR_BACKEND", "google")
    mocker.patch("app.calendar.poller.build_calendar_service", return_value=mock_service)
    mocker.patch("app.calendar.poller.fetch_events_for_window", return_value=raw_events)
    mocker.patch("app.calendar.poller.GCAL_CALENDAR_ID", "test@group.calendar.google.com")
    mocker.patch("app.calendar.poller.SessionLocal", return_value=db_session)

    from app.calendar.poller import poll_calendar

    result = poll_calendar()

    assert result == 1
    assert db_session.query(CalendarCache).count() == 1


# ---------------------------------------------------------------------------
# test_poll_caldav_writes_content_free_rows (FB-06 regression, Phase 999.30)
# ---------------------------------------------------------------------------

def test_poll_caldav_writes_content_free_rows(db_session, mocker):
    """On the caldav backend, poll writes one content-free CalendarCache row per
    busy interval: summary='' and a UNIQUE event_id, no event content."""

    intervals = [
        (
            datetime(2026, 6, 20, 9, 0, tzinfo=timezone.utc),
            datetime(2026, 6, 20, 10, 0, tzinfo=timezone.utc),
        ),
        (
            datetime(2026, 6, 20, 11, 0, tzinfo=timezone.utc),
            datetime(2026, 6, 20, 12, 0, tzinfo=timezone.utc),
        ),
    ]

    mocker.patch("app.calendar.poller.CALENDAR_BACKEND", "caldav")
    mocker.patch(
        "app.calendar.poller.fetch_busy_intervals_all_calendars",
        return_value=intervals,
    )
    mocker.patch("app.calendar.poller.SessionLocal", return_value=db_session)

    from app.calendar.poller import poll_calendar

    result = poll_calendar()

    assert result == 2
    rows = db_session.query(CalendarCache).all()
    assert len(rows) == 2
    assert all(row.summary == "" for row in rows)            # FB-06: content-free
    assert len({row.event_id for row in rows}) == 2          # unique event_ids


# ---------------------------------------------------------------------------
# test_poll_clears_old_cache
# ---------------------------------------------------------------------------

def test_poll_clears_old_cache(db_session, mocker):
    """Existing CalendarCache rows are deleted before inserting fresh events."""
    # Pre-insert a stale row
    stale = CalendarCache(
        event_id="old-evt",
        summary="Old Lesson",
        start_dt=datetime(2026, 5, 1, 8, 0),
        end_dt=datetime(2026, 5, 1, 10, 0),
    )
    db_session.add(stale)
    db_session.commit()
    assert db_session.query(CalendarCache).count() == 1

    raw_events = [_make_raw_event("new-evt")]
    mock_service = MagicMock()
    mocker.patch("app.calendar.poller.CALENDAR_BACKEND", "google")
    mocker.patch("app.calendar.poller.build_calendar_service", return_value=mock_service)
    mocker.patch("app.calendar.poller.fetch_events_for_window", return_value=raw_events)
    mocker.patch("app.calendar.poller.GCAL_CALENDAR_ID", "test@group.calendar.google.com")
    mocker.patch("app.calendar.poller.SessionLocal", return_value=db_session)

    from app.calendar.poller import poll_calendar

    result = poll_calendar()

    assert result == 1
    rows = db_session.query(CalendarCache).all()
    assert len(rows) == 1
    assert rows[0].event_id == "new-evt"
