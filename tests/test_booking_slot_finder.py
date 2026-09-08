"""Tests for slot finder (BOOK-01, BOOK-02)."""
from datetime import date, datetime, time, timezone
from unittest.mock import patch

import pytest

from app.booking.slot_finder import (
    filter_slots_by_time_of_day,
    find_earliest_slot,
    TIME_OF_DAY_RANGES,
)


def test_time_of_day_ranges_match_d03():
    """D-03: morgen 07-09, formiddag 09-12, eftermiddag 12-16, aften 16-18."""
    assert TIME_OF_DAY_RANGES["morgen"] == (time(7, 0), time(9, 0))
    assert TIME_OF_DAY_RANGES["formiddag"] == (time(9, 0), time(12, 0))
    assert TIME_OF_DAY_RANGES["eftermiddag"] == (time(12, 0), time(16, 0))
    assert TIME_OF_DAY_RANGES["aften"] == (time(16, 0), time(18, 0))


def test_filter_slots_by_time_of_day_morgen():
    """Only slots between 07:00-09:00 kept for morgen."""
    slots = [
        datetime(2026, 4, 15, 6, 30),
        datetime(2026, 4, 15, 7, 0),
        datetime(2026, 4, 15, 8, 30),
        datetime(2026, 4, 15, 9, 0),
        datetime(2026, 4, 15, 10, 0),
    ]
    filtered = filter_slots_by_time_of_day(slots, "morgen")
    assert len(filtered) == 2
    assert filtered[0].hour == 7
    assert filtered[1].hour == 8


def test_filter_slots_by_time_of_day_eftermiddag():
    """Only slots between 12:00-16:00 kept for eftermiddag."""
    slots = [
        datetime(2026, 4, 15, 11, 30),
        datetime(2026, 4, 15, 12, 0),
        datetime(2026, 4, 15, 14, 30),
        datetime(2026, 4, 15, 16, 0),
    ]
    filtered = filter_slots_by_time_of_day(slots, "eftermiddag")
    assert len(filtered) == 2
    assert filtered[0].hour == 12
    assert filtered[1].hour == 14


def test_filter_slots_unknown_time_of_day_returns_all():
    """Unknown time-of-day key returns all slots unfiltered."""
    slots = [datetime(2026, 4, 15, 10, 0)]
    assert filter_slots_by_time_of_day(slots, "nat") == slots


@patch("app.booking.slot_finder.refresh_if_stale")
@patch("app.booking.slot_finder.get_slots_for_date")
@patch("app.booking.slot_finder.load_appointment_types")
def test_find_earliest_slot_returns_first_available(mock_load, mock_get, mock_refresh):
    """find_earliest_slot returns the first slot from the first day with availability."""
    from app.calendar.slot_engine import AppointmentType
    mock_load.return_value = {"lesson": AppointmentType("lesson", 90, 15)}
    # Day 1: no slots, Day 2: has slots
    mock_get.side_effect = [[], [datetime(2026, 4, 16, 8, 15)]]

    result = find_earliest_slot("lesson", max_days_ahead=3)
    assert result == datetime(2026, 4, 16, 8, 15)


@patch("app.booking.slot_finder.refresh_if_stale")
@patch("app.booking.slot_finder.get_slots_for_date")
@patch("app.booking.slot_finder.load_appointment_types")
def test_find_earliest_slot_returns_none_when_no_slots(mock_load, mock_get, mock_refresh):
    """find_earliest_slot returns None when no slots in search window."""
    from app.calendar.slot_engine import AppointmentType
    mock_load.return_value = {"lesson": AppointmentType("lesson", 90, 15)}
    mock_get.return_value = []

    result = find_earliest_slot("lesson", max_days_ahead=2)
    assert result is None


# ---------------------------------------------------------------------------
# Day-walk anchor must be Copenhagen-local, not the container's system tz
# (deep code review 2026-07-06, F1). Railway sets no TZ, so it runs UTC.
# ---------------------------------------------------------------------------


@pytest.fixture
def _utc_2230_on_27_jul(monkeypatch):
    """Freeze the clock at 22:30 UTC on 27 Jul 2026 — 00:30 on the 28th in
    Copenhagen (CEST, UTC+2). This is the window where the two calendars
    disagree, and the only window where the bug could fire.

    `date` is patched as well as `datetime`, so `date.today()` reports the UTC
    day exactly as a Railway container would. Without that these tests pass
    against the buggy code whenever the developer's own machine happens to sit
    on the later date — which is precisely how this bug survived review.
    """
    from app.booking import slot_finder
    from app.calendar import slot_engine

    instant = datetime(2026, 7, 27, 22, 30, tzinfo=timezone.utc)

    class _Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return instant.astimezone(tz) if tz else instant.replace(tzinfo=None)

    class _UtcDate(date):
        @classmethod
        def today(cls):
            return date(2026, 7, 27)      # what the UTC container reports

    # Freeze at the SOURCE: `_today_copenhagen` derives from
    # `slot_engine._now_local()` so the day-walk and the front-edge filter share
    # one clock. Freezing slot_finder's own `datetime` alone would no longer
    # reach it — and a fixture that silently stops controlling the clock is how
    # a green test starts proving nothing.
    monkeypatch.setattr(slot_engine, "datetime", _Frozen)
    monkeypatch.setattr(slot_finder, "datetime", _Frozen)
    # Left patched deliberately: if anything here ever reaches for `date.today()`
    # again it reports the UTC day, and these tests fail — which is the whole
    # bug. Both modules, since either could regress.
    monkeypatch.setattr(slot_finder, "date", _UtcDate)
    monkeypatch.setattr(slot_engine, "date", _UtcDate)
    return instant


def test_today_copenhagen_follows_copenhagen_not_utc(_utc_2230_on_27_jul):
    """The anchor must be Copenhagen's date. `date.today()` on a UTC box would
    return the 27th here, putting the day-walk a full day behind the
    Copenhagen-local front-edge filter inside get_slots_for_date."""
    from app.booking.slot_finder import _today_copenhagen

    assert _today_copenhagen() == date(2026, 7, 28)      # Copenhagen: already tomorrow
    assert _utc_2230_on_27_jul.date() == date(2026, 7, 27)   # UTC: still yesterday


def test_earliest_slots_walk_starts_from_copenhagen_tomorrow(_utc_2230_on_27_jul):
    """End-to-end consequence: the first date queried is Copenhagen-tomorrow
    (29 Jul), not UTC-tomorrow (28 Jul). Anchoring on UTC would re-offer a day
    Copenhagen has already begun."""
    from app.booking import slot_finder

    queried: list[date] = []

    def _record(target, appt_type, session=None):
        queried.append(target)
        return []

    with patch.object(slot_finder, "get_slots_for_date", _record), \
         patch.object(slot_finder, "load_appointment_types",
                      return_value={"lesson": object()}), \
         patch.object(slot_finder, "refresh_if_stale", lambda: None):
        slot_finder.find_earliest_slots("lesson", n=1, max_days_ahead=3)

    assert queried[0] == date(2026, 7, 29)
    assert queried == [date(2026, 7, 29), date(2026, 7, 30), date(2026, 7, 31)]


def test_day_walk_anchor_follows_the_slot_engine_clock(monkeypatch):
    """One clock, not two: freezing `slot_engine._now_local` alone must move the
    day-walk anchor with it.

    The day-walk and the front-edge filter inside `get_slots_for_date` must
    agree on what "today" is — them disagreeing IS the bug this module's
    `_today_copenhagen` exists to prevent. Two independent clock reads let a
    test freeze one and not the other and then assert against a state that
    cannot occur in production, which is how a wrong-day bug hides behind a
    green suite.
    """
    from app.booking.slot_finder import _today_copenhagen
    from app.calendar import slot_engine

    monkeypatch.setattr(
        slot_engine, "_now_local", lambda: datetime(2026, 3, 9, 23, 45)
    )
    assert _today_copenhagen() == date(2026, 3, 9)

    monkeypatch.setattr(
        slot_engine, "_now_local", lambda: datetime(2026, 12, 31, 0, 15)
    )
    assert _today_copenhagen() == date(2026, 12, 31)
