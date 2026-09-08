"""Tests for the CalDAV calendar client (iCloud-targeted).

These pin the URL-based create/delete contract that was forced on us by
iCloud rejecting the calendar-query REPORT used by ``event_by_uid`` with
412 Precondition Failed (see commit 2cb6b9c). The ``caldav`` library is
fully mocked — these are pure unit tests of our serialization,
identifier handling, and dispatcher routing.

Live round-trip coverage lives in ``scripts/test_caldav.py`` and must be
run manually against a real iCloud account.
"""
from datetime import date, datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from icalendar import Calendar, Event


# ---------------------------------------------------------------------------
# Shared fixtures for the privacy-preserving multi-calendar busy-read
# (Phase 999.30). These deliberately carry SUMMARY/LOCATION so FB-06 can prove
# the extraction strips all content and returns only (start, end) tuples.
# ---------------------------------------------------------------------------


class _FakeSearchResult:
    """Mirror of a caldav search result: exposes `.icalendar_instance`."""

    def __init__(self, calendar: Calendar):
        self.icalendar_instance = calendar


def _busy_fixture_calendar() -> Calendar:
    """A Calendar with 4 VEVENTs exercising FB-04/05/06.

    (a) timed + OPAQUE              -> busy, included
    (b) timed + TRANSP=TRANSPARENT  -> free, excluded (FB-05)
    (c) all-day (DATE-valued)       -> excluded (FB-04)  [on a DIFFERENT date]
    (d) timed + NO transp property  -> defaults busy, included (FB-05)

    Every event carries a distinctive SUMMARY + LOCATION so FB-06 can assert
    none of that content leaks into the returned intervals.
    """
    cal = Calendar()
    cal.add("prodid", "-//Lisa test fixture//EN")
    cal.add("version", "2.0")

    # (a) timed + OPAQUE -> included
    a = Event()
    a.add("uid", "evt-a@fixture")
    a.add("summary", "SECRET-MEETING-A")
    a.add("location", "Hemmelig adresse A")
    a.add("dtstart", datetime(2026, 6, 20, 9, 0, tzinfo=timezone.utc))
    a.add("dtend", datetime(2026, 6, 20, 10, 0, tzinfo=timezone.utc))
    a.add("transp", "OPAQUE")
    cal.add_component(a)

    # (b) timed + TRANSPARENT -> excluded
    b = Event()
    b.add("uid", "evt-b@fixture")
    b.add("summary", "SECRET-MEETING-B")
    b.add("location", "Hemmelig adresse B")
    b.add("dtstart", datetime(2026, 6, 20, 11, 0, tzinfo=timezone.utc))
    b.add("dtend", datetime(2026, 6, 20, 12, 0, tzinfo=timezone.utc))
    b.add("transp", "TRANSPARENT")
    cal.add_component(b)

    # (c) all-day (DATE-valued) on a different date -> excluded (FB-04)
    c = Event()
    c.add("uid", "evt-c@fixture")
    c.add("summary", "SECRET-MEETING-C")
    c.add("location", "Hemmelig adresse C")
    c.add("dtstart", date(2026, 6, 15))
    c.add("dtend", date(2026, 6, 16))
    cal.add_component(c)

    # (d) timed + NO transp property -> defaults busy, included (FB-05)
    d = Event()
    d.add("uid", "evt-d@fixture")
    d.add("summary", "SECRET-MEETING-D")
    d.add("location", "Hemmelig adresse D")
    d.add("dtstart", datetime(2026, 6, 20, 14, 0, tzinfo=timezone.utc))
    d.add("dtend", datetime(2026, 6, 20, 15, 0, tzinfo=timezone.utc))
    cal.add_component(d)

    return cal


def _single_event_result(uid: str, hour: int) -> _FakeSearchResult:
    """A search result wrapping ONE timed-opaque VEVENT (for enumeration tests)."""
    cal = Calendar()
    cal.add("prodid", "-//Lisa test fixture//EN")
    cal.add("version", "2.0")
    ev = Event()
    ev.add("uid", uid)
    ev.add("summary", f"SECRET-{uid}")
    ev.add("dtstart", datetime(2026, 6, 20, hour, 0, tzinfo=timezone.utc))
    ev.add("dtend", datetime(2026, 6, 20, hour + 1, 0, tzinfo=timezone.utc))
    cal.add_component(ev)
    return _FakeSearchResult(cal)


# ---------------------------------------------------------------------------
# _extract_busy_intervals — content-free per-event extraction (FB-04/05/06)
# ---------------------------------------------------------------------------


def test_extract_busy_intervals_skips_all_day_events():
    """FB-04: all-day (DATE-valued) events contribute no interval."""
    from app.calendar.caldav_client import _extract_busy_intervals

    result = _extract_busy_intervals([_FakeSearchResult(_busy_fixture_calendar())])

    # The only event on 2026-06-15 is the all-day one — it must be absent.
    assert len([iv for iv in result if iv[0].date() == date(2026, 6, 15)]) == 0


def test_extract_busy_intervals_honors_transp():
    """FB-05: TRANSPARENT excluded; absent-TRANSP defaults busy (included)."""
    from app.calendar.caldav_client import _extract_busy_intervals

    result = _extract_busy_intervals([_FakeSearchResult(_busy_fixture_calendar())])

    # Included: (a) OPAQUE + (d) no-transp. Excluded: (b) TRANSPARENT, (c) all-day.
    assert len(result) == 2
    starts = {iv[0].hour for iv in result}
    assert 9 in starts   # (a) OPAQUE
    assert 14 in starts  # (d) no transp -> defaults busy
    assert 11 not in starts  # (b) TRANSPARENT excluded


def test_extract_busy_intervals_returns_only_intervals():
    """FB-06: returns only (datetime, datetime) tuples; no content leaks."""
    from app.calendar.caldav_client import _extract_busy_intervals

    result = _extract_busy_intervals([_FakeSearchResult(_busy_fixture_calendar())])

    assert all(isinstance(iv, tuple) and len(iv) == 2 for iv in result)
    assert all(isinstance(iv[0], datetime) and isinstance(iv[1], datetime) for iv in result)
    # The content-bearing SUMMARY/LOCATION must never appear in the structure.
    assert "SECRET-MEETING-A" not in repr(result)
    assert "Hemmelig adresse" not in repr(result)


# ---------------------------------------------------------------------------
# create_event — must return server-assigned URL, not the iCal UID
# ---------------------------------------------------------------------------


def test_create_event_returns_server_url_not_uid():
    """create_event captures saved.url and returns it as the identifier."""
    from app.calendar.caldav_client import create_event

    fake_url = (
        "https://caldav.icloud.com/123/calendars/ABC/"
        "5234f2e8-af6f-47d7-9862-821278f37ae8%40lisa.ics"
    )
    fake_calendar = MagicMock()
    saved_event = MagicMock()
    saved_event.url = fake_url
    fake_calendar.save_event.return_value = saved_event

    start = datetime(2026, 5, 20, 10, 0, tzinfo=timezone.utc)
    end = datetime(2026, 5, 20, 10, 45, tzinfo=timezone.utc)

    result = create_event(
        fake_calendar, "_ignored", "Køretime - Test", start, end, "desc"
    )

    assert result == {"id": fake_url}
    assert fake_calendar.save_event.call_count == 1
    # The iCal payload sent to save_event must contain the summary and dtstart.
    sent_ical = fake_calendar.save_event.call_args[0][0]
    assert "SUMMARY:Køretime - Test" in sent_ical
    assert "BEGIN:VEVENT" in sent_ical


def test_create_event_uid_is_lisa_namespaced():
    """Generated UID is uuid + '@lisa' suffix so we can recognise own events later."""
    from app.calendar.caldav_client import create_event

    fake_calendar = MagicMock()
    saved = MagicMock()
    saved.url = "https://example/cal/x.ics"
    fake_calendar.save_event.return_value = saved

    create_event(
        fake_calendar,
        "_ignored",
        "x",
        datetime(2026, 5, 20, 10, 0, tzinfo=timezone.utc),
        datetime(2026, 5, 20, 10, 30, tzinfo=timezone.utc),
    )

    sent_ical = fake_calendar.save_event.call_args[0][0]
    assert "@lisa" in sent_ical


# ---------------------------------------------------------------------------
# delete_event — must reconstruct an Event by URL and delete, never use UID
# ---------------------------------------------------------------------------


LISA_COLLECTION = "https://caldav.icloud.com/1234/calendars/lisa/"


def test_delete_event_uses_url_not_event_by_uid():
    """delete_event constructs caldav.Event(parent=service, url=event_id) and
    calls .delete() — must NOT call service.event_by_uid (which iCloud 412s)."""
    from app.calendar import caldav_client

    fake_calendar = MagicMock()
    fake_calendar.url = LISA_COLLECTION
    fake_event_instance = MagicMock()
    target_url = LISA_COLLECTION + "abc.ics"

    with patch("caldav.Event", return_value=fake_event_instance) as event_ctor:
        caldav_client.delete_event(fake_calendar, "_ignored", target_url)

    event_ctor.assert_called_once_with(parent=fake_calendar, url=target_url)
    fake_event_instance.delete.assert_called_once()
    # The bug-prone path must remain unused.
    fake_calendar.event_by_uid.assert_not_called()


def test_delete_event_refuses_url_outside_lisa_collection():
    """The safety rule that matters: Lisa shares credentials with the owner's
    personal calendars, and a CalDAV delete is permanent. A URL from any other
    collection must be refused before caldav.Event is even constructed."""
    from app.calendar import caldav_client

    fake_calendar = MagicMock()
    fake_calendar.url = LISA_COLLECTION
    foreign = "https://caldav.icloud.com/1234/calendars/personal/private.ics"

    with patch("caldav.Event") as event_ctor:
        with pytest.raises(caldav_client.CalendarSafetyError):
            caldav_client.delete_event(fake_calendar, "_ignored", foreign)

    event_ctor.assert_not_called()          # refused before touching the network
    fake_calendar.event_by_uid.assert_not_called()


def test_delete_event_refuses_when_collection_url_is_unknown():
    """A service object with no usable URL must fail closed, not fall through to
    an unguarded delete — otherwise a mock or a changed caldav API silently
    disables the guard."""
    from app.calendar import caldav_client

    fake_calendar = MagicMock()
    fake_calendar.url = ""

    with patch("caldav.Event") as event_ctor:
        with pytest.raises(caldav_client.CalendarSafetyError):
            caldav_client.delete_event(fake_calendar, "_ignored", "https://x/y/a.ics")

    event_ctor.assert_not_called()


def test_delete_event_tolerates_missing_trailing_slash_on_collection():
    """caldav may hand back the collection URL with or without a trailing slash;
    a legitimate delete must not be refused over that cosmetic difference."""
    from app.calendar import caldav_client

    fake_calendar = MagicMock()
    fake_calendar.url = LISA_COLLECTION.rstrip("/")     # no trailing slash
    fake_event_instance = MagicMock()

    with patch("caldav.Event", return_value=fake_event_instance):
        caldav_client.delete_event(fake_calendar, "_ignored", LISA_COLLECTION + "ok.ics")

    fake_event_instance.delete.assert_called_once()


def test_delete_event_refuses_sibling_collection_with_shared_prefix():
    """"…/lisa-private/" must not pass a prefix check against "…/lisa/" — the
    normalised trailing slash is what makes the boundary a real one."""
    from app.calendar import caldav_client

    fake_calendar = MagicMock()
    fake_calendar.url = LISA_COLLECTION
    sibling = "https://caldav.icloud.com/1234/calendars/lisa-private/x.ics"

    with patch("caldav.Event") as event_ctor:
        with pytest.raises(caldav_client.CalendarSafetyError):
            caldav_client.delete_event(fake_calendar, "_ignored", sibling)

    event_ctor.assert_not_called()


# ---------------------------------------------------------------------------
# build_calendar_service — uses get_display_name(), not the deprecated .name
# ---------------------------------------------------------------------------


def test_build_calendar_service_matches_by_display_name():
    """The connect path must compare against get_display_name() (not the
    deprecated .name attribute) so iCloud connect doesn't emit warnings
    AND so display-name-only calendars still match."""
    from app.calendar import caldav_client

    target_cal = MagicMock()
    target_cal.get_display_name.return_value = "Lisa-test"

    other_cal = MagicMock()
    other_cal.get_display_name.return_value = "Home"

    fake_principal = MagicMock()
    fake_principal.calendars.return_value = [other_cal, target_cal]

    fake_client = MagicMock()
    fake_client.principal.return_value = fake_principal

    with patch("app.config.CALDAV_URL", "https://caldav.icloud.com/"):
        with patch("app.config.CALDAV_USERNAME", "x@example.com"):
            with patch("app.config.CALDAV_PASSWORD", "pw"):
                with patch("app.config.CALDAV_CALENDAR_NAME", "Lisa-test"):
                    with patch("caldav.DAVClient", return_value=fake_client):
                        result = caldav_client.build_calendar_service()

    assert result is target_cal


def test_build_calendar_service_missing_creds_raises():
    """Empty creds → ValueError with actionable message."""
    from app.calendar import caldav_client

    with patch("app.config.CALDAV_URL", ""):
        with patch("app.config.CALDAV_USERNAME", ""):
            with patch("app.config.CALDAV_PASSWORD", ""):
                with pytest.raises(ValueError, match="CALDAV"):
                    caldav_client.build_calendar_service()


def test_build_calendar_service_calendar_not_found_lists_alternatives():
    """If the configured calendar name isn't found, the error includes the
    list of available calendars (so the operator can spot typos like
    Lisa-test vs Lisa Test)."""
    from app.calendar import caldav_client

    cal_a = MagicMock()
    cal_a.get_display_name.return_value = "Home"
    cal_b = MagicMock()
    cal_b.get_display_name.return_value = "Work"

    fake_principal = MagicMock()
    fake_principal.calendars.return_value = [cal_a, cal_b]

    fake_client = MagicMock()
    fake_client.principal.return_value = fake_principal

    with patch("app.config.CALDAV_URL", "https://caldav.icloud.com/"):
        with patch("app.config.CALDAV_USERNAME", "x@example.com"):
            with patch("app.config.CALDAV_PASSWORD", "pw"):
                with patch("app.config.CALDAV_CALENDAR_NAME", "Lisa"):
                    with patch("caldav.DAVClient", return_value=fake_client):
                        with pytest.raises(ValueError) as exc:
                            caldav_client.build_calendar_service()

    msg = str(exc.value)
    assert "'Lisa'" in msg
    assert "Home" in msg and "Work" in msg


# ---------------------------------------------------------------------------
# Round-trip — create returns URL, delete consumes URL
# ---------------------------------------------------------------------------


def test_create_then_delete_roundtrip_uses_same_url():
    """End-to-end identifier handshake: create's returned id flows into
    delete and is used as the URL to construct caldav.Event."""
    from app.calendar import caldav_client

    fake_calendar = MagicMock()
    fake_calendar.url = LISA_COLLECTION
    saved = MagicMock()
    # A real server assigns the event URL *under* the calendar's own collection,
    # which is exactly what lets delete_event verify it before deleting.
    saved.url = LISA_COLLECTION + "ev.ics"
    fake_calendar.save_event.return_value = saved

    created = caldav_client.create_event(
        fake_calendar,
        "_ignored",
        "Lisa booking",
        datetime(2026, 5, 20, 10, 0, tzinfo=timezone.utc),
        datetime(2026, 5, 20, 10, 45, tzinfo=timezone.utc),
    )

    fake_event = MagicMock()
    with patch("caldav.Event", return_value=fake_event) as event_ctor:
        caldav_client.delete_event(fake_calendar, "_ignored", created["id"])

    event_ctor.assert_called_once_with(parent=fake_calendar, url=saved.url)
    fake_event.delete.assert_called_once()


# ---------------------------------------------------------------------------
# fetch_busy_intervals_all_calendars — enumerate ALL calendars (FB-01/FB-07)
# ---------------------------------------------------------------------------


def _mock_calendar(name: str, result_hour: int):
    """A mock caldav.Calendar whose .search() returns one timed-opaque event."""
    cal = MagicMock()
    cal.get_display_name.return_value = name
    cal.search.return_value = [_single_event_result(f"{name}-evt", result_hour)]
    return cal


def _patched_creds():
    """Context-manager stack patching CalDAV creds + a fake DAVClient factory."""
    return (
        patch("app.config.CALDAV_URL", "https://caldav.icloud.com/"),
        patch("app.config.CALDAV_USERNAME", "x@example.com"),
        patch("app.config.CALDAV_PASSWORD", "pw"),
    )


def test_fetch_busy_intervals_iterates_all_calendars():
    """FB-01: every calendar on the principal is searched, not just one."""
    from app.calendar.caldav_client import fetch_busy_intervals_all_calendars

    cals = [_mock_calendar("Cal A", 9), _mock_calendar("Cal B", 11), _mock_calendar("Cal C", 13)]
    fake_principal = MagicMock()
    fake_principal.calendars.return_value = cals
    fake_client = MagicMock()
    fake_client.principal.return_value = fake_principal

    url_p, user_p, pw_p = _patched_creds()
    with url_p, user_p, pw_p, patch("caldav.DAVClient", return_value=fake_client):
        result = fetch_busy_intervals_all_calendars(days_ahead=30)

    # One interval per calendar (each returned a single timed-opaque event).
    assert len(result) == 3
    for cal in cals:
        cal.search.assert_called()  # ALL calendars searched (FB-01)


def test_fetch_busy_intervals_skips_failing_calendar():
    """FB-07: one calendar's search() raising does not abort the whole fetch."""
    from app.calendar.caldav_client import fetch_busy_intervals_all_calendars

    good_a = _mock_calendar("Cal A", 9)
    bad = MagicMock()
    bad.get_display_name.return_value = "Broken"
    bad.search.side_effect = Exception("REPORT not supported")
    good_c = _mock_calendar("Cal C", 13)
    cals = [good_a, bad, good_c]

    fake_principal = MagicMock()
    fake_principal.calendars.return_value = cals
    fake_client = MagicMock()
    fake_client.principal.return_value = fake_principal

    url_p, user_p, pw_p = _patched_creds()
    with url_p, user_p, pw_p, patch("caldav.DAVClient", return_value=fake_client):
        result = fetch_busy_intervals_all_calendars(days_ahead=30)

    # Intervals only from the two healthy calendars; no exception propagated.
    assert len(result) == 2
    for cal in cals:
        cal.search.assert_called()  # all 3 attempted, including the failing one


# ---------------------------------------------------------------------------
# Dispatcher — selects caldav backend when configured
# ---------------------------------------------------------------------------


def test_dispatcher_routes_to_caldav_when_backend_is_caldav():
    """app.calendar.client must select caldav_client when CALENDAR_BACKEND=caldav.

    The dispatcher resolves at module load, so we re-import the module under
    a patched backend value.
    """
    import importlib
    import sys

    with patch("app.config.CALENDAR_BACKEND", "caldav"):
        if "app.calendar.client" in sys.modules:
            del sys.modules["app.calendar.client"]
        client_mod = importlib.import_module("app.calendar.client")
        from app.calendar import caldav_client as expected

        assert client_mod.create_event is expected.create_event
        assert client_mod.delete_event is expected.delete_event
        assert client_mod.build_calendar_service is expected.build_calendar_service

    # Restore default routing for the rest of the suite.
    with patch("app.config.CALENDAR_BACKEND", "google"):
        if "app.calendar.client" in sys.modules:
            del sys.modules["app.calendar.client"]
        importlib.import_module("app.calendar.client")


def test_calendar_safety_error_keeps_one_identity_across_dispatcher_reimport():
    """CalendarSafetyError must be the SAME class object under either backend,
    and must survive a re-import of the dispatcher.

    Modules that did `from app.calendar.client import CalendarSafetyError` at
    import time hold a reference forever. If the dispatcher defined its own
    parallel class, re-importing it would mint a second class and every
    `except CalendarSafetyError` elsewhere would quietly stop matching — the
    guard would look present and catch nothing.
    """
    import importlib
    import sys

    from app.calendar import caldav_client

    for backend in ("caldav", "google"):
        with patch("app.config.CALENDAR_BACKEND", backend):
            if "app.calendar.client" in sys.modules:
                del sys.modules["app.calendar.client"]
            client_mod = importlib.import_module("app.calendar.client")
            assert client_mod.CalendarSafetyError is caldav_client.CalendarSafetyError, (
                f"dispatcher minted a second CalendarSafetyError under {backend!r}"
            )

    # Restore default routing for the rest of the suite.
    with patch("app.config.CALENDAR_BACKEND", "google"):
        if "app.calendar.client" in sys.modules:
            del sys.modules["app.calendar.client"]
        importlib.import_module("app.calendar.client")
