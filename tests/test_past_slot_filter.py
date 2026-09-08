"""Regression tests for the past-time slot bug (pilot 2026-06-10).

the owner asked for a same-day slot, was offered a time 30 min in the past, and
booked it. Root cause: compute_available_slots builds from WORK_START regardless
of the current time, and the single-day target_date path (find_slots_for_date)
reaches get_slots_for_date with today and had no `now` filter.

Fix under test: get_slots_for_date never returns a slot <= now. This is the
chokepoint for every finder path, and only bites for target_date == today (the
multi-day finders already start at tomorrow; future dates are unaffected).

DONE (follow-up, 2026-06-11): the defence-in-depth past-guard in _confirm_booking
/ _handle_reschedule_confirm is now implemented (see tests/test_confirm_guards.py).
The time-travelling-test problem (the pressure suite mocks finders with hardcoded
datetime(2026, 5, 20, ...) slots) was solved without de-rotting ~46 tests: the
guard reads now via a patchable `_now_local()` seam, and an autouse conftest
fixture freezes that seam to 2026-01-01 for the whole suite — a no-op for every
existing assertion since nothing else uses `_now_local`. This closes the
confirm-step variant: a student who delays "ja" until after the offered slot has
passed is now rejected, not booked into the past.
"""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.calendar.slot_engine import AppointmentType, get_slots_for_date

COPENHAGEN_TZ = ZoneInfo("Europe/Copenhagen")


def _now_local() -> datetime:
    return datetime.now(COPENHAGEN_TZ).replace(tzinfo=None)


def _lesson() -> AppointmentType:
    return AppointmentType(name="lesson", duration_minutes=90, buffer_minutes=15)


def test_get_slots_for_date_excludes_past_slots_today(db_session):
    """No slot returned for *today* may start at or before the current time."""
    today = _now_local().date()
    slots = get_slots_for_date(today, _lesson(), session=db_session)
    now = _now_local()
    assert all(s > now for s in slots), (
        f"get_slots_for_date returned a past slot for today: "
        f"{[s for s in slots if s <= now]} (now={now})"
    )


def test_get_slots_for_date_future_date_unaffected(db_session):
    """The past filter is a no-op for future dates — slots are still produced."""
    future = (_now_local() + timedelta(days=3)).date()
    slots = get_slots_for_date(future, _lesson(), session=db_session)
    assert slots, "expected available slots on an empty future date"
    assert all(s > _now_local() for s in slots)
