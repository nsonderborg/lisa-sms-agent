"""Minimum-notice front-edge filter — Lisa won't offer a slot sooner than
MIN_NOTICE_HOURS from now, so a same-day "ASAP" request can't land minutes away
on the instructor's unblocked time.

Surfaced during the pilot 2026-06-15: the only front-edge guard was "not in the
past", so a free afternoon could be booked 15 min out.
"""
from datetime import date, datetime, timedelta

import pytest

from app.calendar import slot_engine


@pytest.fixture
def free_today(monkeypatch):
    """Freeze 'now' mid-morning on a fixed day with an empty calendar + wide hours."""
    fixed = datetime(2026, 6, 15, 10, 0)  # naive Copenhagen-local
    monkeypatch.setattr(slot_engine, "_now_local", lambda: fixed)
    monkeypatch.setattr(slot_engine, "get_busy_intervals", lambda d, session=None: [])
    monkeypatch.setattr(slot_engine, "WORK_START", "07:00")
    monkeypatch.setattr(slot_engine, "WORK_END", "18:00")
    appt = slot_engine.load_appointment_types()["lesson"]
    return fixed, appt


def _slots(appt, notice, monkeypatch):
    monkeypatch.setattr(slot_engine, "MIN_NOTICE_HOURS", notice)
    return slot_engine.get_slots_for_date(date(2026, 6, 15), appt)


def test_no_notice_offers_near_slots(free_today, monkeypatch):
    fixed, appt = free_today
    base = _slots(appt, 0, monkeypatch)
    assert base, "expected slots on an empty wide-hours day"
    # With no notice, the earliest offered slot sits soon after 'now'
    assert base[0] < fixed + timedelta(hours=3)


def test_min_notice_filters_imminent_slots(free_today, monkeypatch):
    fixed, appt = free_today
    base = _slots(appt, 0, monkeypatch)
    guarded = _slots(appt, 3, monkeypatch)
    # Every guarded slot is at least the notice window away…
    assert all(s >= fixed + timedelta(hours=3) for s in guarded)
    # …and the guard actually removed the near ones that no-notice would offer.
    assert len(guarded) < len(base)


def test_min_notice_keeps_past_slot_guard(free_today, monkeypatch):
    """Even with notice=0, slots at/just before 'now' are still excluded."""
    fixed, appt = free_today
    base = _slots(appt, 0, monkeypatch)
    assert all(s > fixed for s in base)


def _floor_setup(monkeypatch, floor):
    fixed = datetime(2026, 6, 15, 10, 0)
    monkeypatch.setattr(slot_engine, "_now_local", lambda: fixed)
    monkeypatch.setattr(slot_engine, "get_busy_intervals", lambda d, session=None: [])
    monkeypatch.setattr(slot_engine, "WORK_START", "07:00")
    monkeypatch.setattr(slot_engine, "WORK_END", "18:00")
    monkeypatch.setattr(slot_engine, "MIN_NOTICE_HOURS", 0)
    monkeypatch.setattr(slot_engine, "BOOKING_FLOOR_DATE", floor)
    monkeypatch.setattr(slot_engine, "BOOKING_CEILING_DATE", None)  # ceiling off by default
    return slot_engine.load_appointment_types()["lesson"]


def test_booking_floor_excludes_dates_before_floor(monkeypatch):
    appt = _floor_setup(monkeypatch, date(2026, 6, 22))
    assert slot_engine.get_slots_for_date(date(2026, 6, 18), appt) == []  # before floor
    on_floor = slot_engine.get_slots_for_date(date(2026, 6, 22), appt)
    assert on_floor and on_floor[0].date() == date(2026, 6, 22)  # floor day bookable
    assert on_floor[0].hour == 7  # from start of the work day


def test_booking_floor_off_when_none(monkeypatch):
    appt = _floor_setup(monkeypatch, None)
    assert slot_engine.get_slots_for_date(date(2026, 6, 18), appt)  # 18th open when no floor


def test_booking_ceiling_excludes_on_and_after(monkeypatch):
    appt = _floor_setup(monkeypatch, None)
    monkeypatch.setattr(slot_engine, "BOOKING_CEILING_DATE", date(2026, 6, 29))
    before = slot_engine.get_slots_for_date(date(2026, 6, 26), appt)
    assert before and all(s.date() == date(2026, 6, 26) for s in before)  # day before ceiling open
    assert slot_engine.get_slots_for_date(date(2026, 6, 29), appt) == []  # ceiling day excluded
    assert slot_engine.get_slots_for_date(date(2026, 7, 1), appt) == []   # after ceiling excluded


def test_booking_ceiling_off_when_none(monkeypatch):
    appt = _floor_setup(monkeypatch, None)  # ceiling None by default
    assert slot_engine.get_slots_for_date(date(2026, 7, 1), appt)  # far date open when no ceiling


def test_floor_and_ceiling_form_a_window(monkeypatch):
    appt = _floor_setup(monkeypatch, date(2026, 6, 22))
    monkeypatch.setattr(slot_engine, "BOOKING_CEILING_DATE", date(2026, 6, 29))
    assert slot_engine.get_slots_for_date(date(2026, 6, 18), appt) == []  # before floor
    assert slot_engine.get_slots_for_date(date(2026, 6, 22), appt)        # floor day open
    assert slot_engine.get_slots_for_date(date(2026, 6, 28), appt)        # within window (Sun)
    assert slot_engine.get_slots_for_date(date(2026, 6, 29), appt) == []  # ceiling day excluded


def test_future_day_unaffected_by_small_notice(monkeypatch):
    """A few hours' notice never touches slots on a future day."""
    fixed = datetime(2026, 6, 15, 10, 0)
    monkeypatch.setattr(slot_engine, "_now_local", lambda: fixed)
    monkeypatch.setattr(slot_engine, "get_busy_intervals", lambda d, session=None: [])
    monkeypatch.setattr(slot_engine, "WORK_START", "07:00")
    monkeypatch.setattr(slot_engine, "WORK_END", "18:00")
    appt = slot_engine.load_appointment_types()["lesson"]
    monkeypatch.setattr(slot_engine, "MIN_NOTICE_HOURS", 3)
    tomorrow = slot_engine.get_slots_for_date(date(2026, 6, 16), appt)
    assert tomorrow and tomorrow[0].hour == 7  # earliest is the start of the work day
