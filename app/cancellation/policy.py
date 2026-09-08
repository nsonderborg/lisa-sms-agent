"""Cancellation policy engine: working-day deadline calculation (D-03, D-04)."""
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from app.config import APPOINTMENT_TYPES

COPENHAGEN_TZ = ZoneInfo("Europe/Copenhagen")


def subtract_working_days(from_date: date, days: int) -> date:
    """Return the date that is `days` working days (Mon-Fri) before from_date.

    Per D-04: working days = weekdays only. Danish public holidays excluded in MVP.
    """
    current = from_date
    remaining = days
    while remaining > 0:
        current -= timedelta(days=1)
        if current.weekday() < 5:  # Mon=0, Fri=4
            remaining -= 1
    return current


def is_within_cancellation_policy(
    appointment_dt: datetime,
    appointment_type: str,
    now: datetime | None = None,
) -> bool:
    """Return True if cancellation is within policy (enough working days' notice).

    Per D-03: deadline = noon on the date that is cancel_notice_days working days
    before the appointment date. cancel_notice_days: lesson=1, manoeuvring=4, test=6.

    Per D-04: working days = Mon-Fri only.

    appointment_dt: naive Copenhagen local datetime (from slot engine).
    now: naive Copenhagen local datetime (injectable for testing).
    """
    if now is None:
        now = datetime.now(COPENHAGEN_TZ).replace(tzinfo=None)

    appt_config = APPOINTMENT_TYPES.get(appointment_type, {})
    notice_days = appt_config.get("cancel_notice_days", 1)

    appt_date = appointment_dt.date()
    deadline_date = subtract_working_days(appt_date, notice_days)
    # Deadline is noon on the deadline date (per D-03)
    deadline_dt = datetime(deadline_date.year, deadline_date.month, deadline_date.day, 12, 0)

    return now <= deadline_dt


def get_cancel_notice_days(appointment_type: str) -> int:
    """Return the cancel_notice_days for a given appointment type."""
    appt_config = APPOINTMENT_TYPES.get(appointment_type, {})
    return appt_config.get("cancel_notice_days", 1)
