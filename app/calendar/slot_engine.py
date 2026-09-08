"""Slot computation engine: derive available booking slots from cached calendar events."""
import logging
from dataclasses import dataclass
from datetime import datetime, date, timedelta, timezone, time
from zoneinfo import ZoneInfo

from app.config import (
    APPOINTMENT_TYPES,
    BOOKING_FLOOR_DATE,
    BOOKING_CEILING_DATE,
    MIN_NOTICE_HOURS,
    WORK_START,
    WORK_END,
)
from app.database import SessionLocal
from app.models import Booking, CalendarCache

logger = logging.getLogger(__name__)

COPENHAGEN_TZ = ZoneInfo("Europe/Copenhagen")


def _now_local() -> datetime:
    """Current time as a naive Copenhagen-local datetime (patchable test seam)."""
    return datetime.now(COPENHAGEN_TZ).replace(tzinfo=None)


@dataclass
class AppointmentType:
    name: str
    duration_minutes: int
    buffer_minutes: int  # applied BOTH before and after

    @property
    def slot_minutes(self) -> int:
        """Total time needed: duration + buffer on each side."""
        return self.duration_minutes + 2 * self.buffer_minutes


def load_appointment_types() -> dict[str, AppointmentType]:
    """Load appointment types from config.yaml (via app.config.APPOINTMENT_TYPES)."""
    result = {}
    for name, cfg in APPOINTMENT_TYPES.items():
        result[name] = AppointmentType(
            name=name,
            duration_minutes=cfg["duration_minutes"],
            buffer_minutes=cfg["buffer_minutes"],
        )
    return result


def get_busy_intervals(
    target_date: date,
    session=None,
) -> list[tuple[datetime, datetime]]:
    """Read cached events AND confirmed local bookings for target_date.

    Returns busy intervals as naive Copenhagen-local datetimes.

    Two sources, unioned:
      1. ``CalendarCache`` — naive UTC. Refreshed every 15 min from the configured
         backend (Google or CalDAV). Stale by up to ~15 min between polls.
      2. ``Booking`` (status=confirmed) — naive Copenhagen local. Reflects
         bookings made through Lisa since the last cache refresh; without
         this, a freshly-booked slot stays offerable to the next student
         (or the same student) until the next poll.

    The two sets overlap once the cache catches up, but ``_merge_intervals``
    de-dupes overlapping ranges so double-counting is harmless.
    """
    own_session = session is None
    if own_session:
        session = SessionLocal()
    try:
        local_midnight = datetime(target_date.year, target_date.month, target_date.day, tzinfo=COPENHAGEN_TZ)
        utc_start = local_midnight.astimezone(timezone.utc).replace(tzinfo=None)
        utc_end = (local_midnight + timedelta(days=1)).astimezone(timezone.utc).replace(tzinfo=None)

        cache_rows = (
            session.query(CalendarCache)
            .filter(
                CalendarCache.start_dt < utc_end,
                CalendarCache.end_dt > utc_start,
            )
            .all()
        )

        result: list[tuple[datetime, datetime]] = []
        for row in cache_rows:
            start_local = row.start_dt.replace(tzinfo=timezone.utc).astimezone(COPENHAGEN_TZ).replace(tzinfo=None)
            end_local = row.end_dt.replace(tzinfo=timezone.utc).astimezone(COPENHAGEN_TZ).replace(tzinfo=None)
            result.append((start_local, end_local))

        local_start = local_midnight.replace(tzinfo=None)
        local_end = local_start + timedelta(days=1)
        booking_rows = (
            session.query(Booking)
            .filter(
                Booking.status == "confirmed",
                Booking.start_dt < local_end,
                Booking.end_dt > local_start,
            )
            .all()
        )
        for b in booking_rows:
            result.append((b.start_dt, b.end_dt))

        return result
    finally:
        if own_session:
            session.close()


def compute_available_slots(
    busy_intervals: list[tuple[datetime, datetime]],
    work_start: datetime,
    work_end: datetime,
    appt_type: AppointmentType,
) -> list[datetime]:
    """Compute available slot start times (lesson start, not buffer start).

    Args:
        busy_intervals: List of (start, end) tuples representing busy periods.
        work_start: Start of work window (e.g., 08:00 on target date).
        work_end: End of work window (e.g., 18:00 on target date).
        appt_type: Appointment type with duration and buffer.

    Returns:
        List of datetime objects representing valid lesson start times.
        Slots are spaced at 30-minute granularity.
    """
    merged = _merge_intervals(busy_intervals)
    slots = []
    cursor = work_start

    for busy_start, busy_end in merged:
        if busy_start > cursor:
            _add_slots_in_gap(cursor, busy_start, appt_type, slots)
        cursor = max(cursor, busy_end)

    if cursor < work_end:
        _add_slots_in_gap(cursor, work_end, appt_type, slots)

    return slots


def get_slots_for_date(
    target_date: date,
    appt_type: AppointmentType,
    session=None,
) -> list[datetime]:
    """High-level: get available slots for a date by reading cache and computing gaps.

    Combines get_busy_intervals + compute_available_slots with work hours from config.
    """
    busy = get_busy_intervals(target_date, session=session)

    work_start_time = time.fromisoformat(WORK_START)
    work_end_time = time.fromisoformat(WORK_END)

    work_start_dt = datetime(
        target_date.year, target_date.month, target_date.day,
        work_start_time.hour, work_start_time.minute,
    )
    work_end_dt = datetime(
        target_date.year, target_date.month, target_date.day,
        work_end_time.hour, work_end_time.minute,
    )

    slots = compute_available_slots(busy, work_start_dt, work_end_dt, appt_type)

    # Front-edge filter. Never offer a slot in the past, and — when MIN_NOTICE_HOURS
    # is set — never offer one sooner than that many hours from now. This matters
    # for the today path: the multi-day finders start at tomorrow, but every finder
    # funnels through here, and compute_available_slots builds from WORK_START
    # regardless of the current time. Without it, "book i dag" / "ASAP" can offer a
    # past slot (observed live 2026-06-10) or an imminent one (e.g. 15 min away) on
    # the instructor's unblocked time. Future dates are unaffected when notice is
    # small, since `earliest` precedes all their slots. _now_local is a patchable
    # seam for tests.
    earliest = _now_local() + timedelta(hours=MIN_NOTICE_HOURS)
    # Absolute booking-floor date (hard launch/pause gate): never offer anything
    # before it, regardless of how near 'now' is. Takes the later of the two edges.
    if BOOKING_FLOOR_DATE is not None:
        floor_dt = datetime.combine(BOOKING_FLOOR_DATE, time.min)
        if floor_dt > earliest:
            earliest = floor_dt
    # Absolute booking-ceiling date (hard upper bound, mirrors the floor): never
    # offer anything ON or AFTER it (e.g. owner vacation). Off when None.
    ceiling_dt = (
        datetime.combine(BOOKING_CEILING_DATE, time.min)
        if BOOKING_CEILING_DATE is not None
        else None
    )
    return [
        s for s in slots
        if s > earliest and (ceiling_dt is None or s < ceiling_dt)
    ]


def _merge_intervals(
    intervals: list[tuple[datetime, datetime]],
) -> list[tuple[datetime, datetime]]:
    """Merge overlapping or adjacent intervals."""
    if not intervals:
        return []
    sorted_iv = sorted(intervals, key=lambda x: x[0])
    merged = [sorted_iv[0]]
    for start, end in sorted_iv[1:]:
        if start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _add_slots_in_gap(
    gap_start: datetime,
    gap_end: datetime,
    appt_type: AppointmentType,
    slots: list[datetime],
) -> None:
    """Add valid slot start times within a free gap.

    A slot needs: [buffer | lesson | buffer] to fit.
    The slot start time returned is the LESSON start (after pre-buffer).
    Slots are spaced at 30-minute granularity.
    """
    slot_duration = timedelta(minutes=appt_type.slot_minutes)
    buffer = timedelta(minutes=appt_type.buffer_minutes)
    cursor = gap_start
    while cursor + slot_duration <= gap_end:
        slots.append(cursor + buffer)  # lesson start = after pre-buffer
        cursor += timedelta(minutes=30)  # 30-min granularity
