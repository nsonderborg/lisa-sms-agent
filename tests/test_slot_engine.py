"""Tests for app/calendar/slot_engine.py — slot computation engine."""
# All tests use pure datetime objects (no timezone) matching the DB storage convention.
from datetime import datetime, timedelta, date


from app.calendar.slot_engine import (
    AppointmentType,
    compute_available_slots,
    load_appointment_types,
    get_busy_intervals,
    _merge_intervals,
)
from app.models import CalendarCache


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ws(h: int = 8, m: int = 0) -> datetime:
    """Return a naive datetime for 2026-06-01 at given hour/minute."""
    return datetime(2026, 6, 1, h, m)


def _lesson() -> AppointmentType:
    return AppointmentType("lesson", 90, 15)


def _manoeuvring() -> AppointmentType:
    return AppointmentType("manoeuvring", 180, 30)


def _test_appt() -> AppointmentType:
    return AppointmentType("test", 60, 30)


# ---------------------------------------------------------------------------
# test_appointment_type_slot_minutes
# ---------------------------------------------------------------------------

def test_appointment_type_slot_minutes():
    """slot_minutes = duration + 2 * buffer."""
    assert _lesson().slot_minutes == 120       # 90 + 2*15
    assert _manoeuvring().slot_minutes == 240  # 180 + 2*30
    assert _test_appt().slot_minutes == 120    # 60 + 2*30


# ---------------------------------------------------------------------------
# test_load_appointment_types
# ---------------------------------------------------------------------------

def test_load_appointment_types(mocker):
    """load_appointment_types() returns correct AppointmentType objects from config."""
    fake_config = {
        "lesson": {"duration_minutes": 90, "buffer_minutes": 15},
        "manoeuvring": {"duration_minutes": 180, "buffer_minutes": 30},
        "test": {"duration_minutes": 60, "buffer_minutes": 30},
    }
    mocker.patch("app.calendar.slot_engine.APPOINTMENT_TYPES", fake_config)

    types = load_appointment_types()

    assert set(types.keys()) == {"lesson", "manoeuvring", "test"}
    assert types["lesson"].duration_minutes == 90
    assert types["lesson"].buffer_minutes == 15
    assert types["lesson"].slot_minutes == 120
    assert types["manoeuvring"].slot_minutes == 240
    assert types["test"].slot_minutes == 120


# ---------------------------------------------------------------------------
# test_no_events_all_slots
# ---------------------------------------------------------------------------

def test_no_events_all_slots():
    """With no busy events, first slot is at work_start + buffer; slots are 30min apart."""
    work_start = _ws(8, 0)
    work_end = _ws(18, 0)
    appt = _lesson()  # 90min + 15min buffer each side = 120min total

    slots = compute_available_slots([], work_start, work_end, appt)

    assert len(slots) > 0
    # First slot: gap_start=08:00, lesson starts at gap_start + buffer = 08:15
    assert slots[0] == datetime(2026, 6, 1, 8, 15)
    # Last slot must end (lesson end + post-buffer) at or before work_end
    for slot in slots:
        lesson_end = slot + timedelta(minutes=appt.duration_minutes)
        post_buffer_end = lesson_end + timedelta(minutes=appt.buffer_minutes)
        assert post_buffer_end <= work_end, f"Slot at {slot} overruns work_end"
    # Slots are 30 minutes apart
    for i in range(len(slots) - 1):
        assert slots[i + 1] - slots[i] == timedelta(minutes=30)


# ---------------------------------------------------------------------------
# test_busy_event_blocks_slots
# ---------------------------------------------------------------------------

def test_busy_event_blocks_slots():
    """No slot overlaps with a busy event (including buffers)."""
    work_start = _ws(8, 0)
    work_end = _ws(18, 0)
    appt = _lesson()  # buffer=15min

    busy = [(_ws(10, 0), _ws(11, 0))]
    slots = compute_available_slots(busy, work_start, work_end, appt)

    buffer = timedelta(minutes=appt.buffer_minutes)
    duration = timedelta(minutes=appt.duration_minutes)
    busy_start, busy_end = busy[0]

    for slot in slots:
        # The full occupied window is: (slot - buffer) to (slot + duration + buffer)
        window_start = slot - buffer
        window_end = slot + duration + buffer
        # Must not overlap [busy_start, busy_end)
        overlaps = window_start < busy_end and window_end > busy_start
        assert not overlaps, f"Slot {slot} overlaps busy period {busy_start}-{busy_end}"


# ---------------------------------------------------------------------------
# test_overlapping_events_merged
# ---------------------------------------------------------------------------

def test_overlapping_events_merged():
    """_merge_intervals merges overlapping intervals into a single block."""
    iv1 = (_ws(9, 0), _ws(10, 30))
    iv2 = (_ws(10, 0), _ws(11, 0))

    merged = _merge_intervals([iv1, iv2])

    assert merged == [(_ws(9, 0), _ws(11, 0))]


# ---------------------------------------------------------------------------
# test_all_day_event_blocks_day
# ---------------------------------------------------------------------------

def test_all_day_event_blocks_day():
    """An all-day busy event (covering full work window) produces no slots."""
    work_start = _ws(8, 0)
    work_end = _ws(18, 0)
    appt = _lesson()

    # All-day event: 00:00 on target day to 00:00 next day
    all_day_start = datetime(2026, 6, 1, 0, 0)
    all_day_end = datetime(2026, 6, 2, 0, 0)

    slots = compute_available_slots([(all_day_start, all_day_end)], work_start, work_end, appt)

    assert slots == []


# ---------------------------------------------------------------------------
# test_buffer_blocks_adjacent_slot
# ---------------------------------------------------------------------------

def test_buffer_blocks_adjacent_slot():
    """Slot cannot start until buffer time has passed since busy event end."""
    work_start = _ws(8, 0)
    work_end = _ws(18, 0)
    appt = _lesson()  # buffer=15min

    # Busy event ends at 10:00; gap starts at 10:00
    busy = [(_ws(9, 0), _ws(10, 0))]
    slots = compute_available_slots(busy, work_start, work_end, appt)

    # First slot after the busy period: gap_start=10:00, lesson_start=10:00+15min=10:15
    post_busy_slots = [s for s in slots if s >= _ws(10, 0)]
    assert len(post_busy_slots) > 0
    assert post_busy_slots[0] == _ws(10, 15)


# ---------------------------------------------------------------------------
# test_lesson_requires_120min_gap
# ---------------------------------------------------------------------------

def test_lesson_requires_120min_gap():
    """A gap shorter than slot_minutes (120) produces no slots."""
    work_start = _ws(8, 0)
    work_end = _ws(8, 0) + timedelta(minutes=110)
    appt = _lesson()  # needs 120min

    slots = compute_available_slots([], work_start, work_end, appt)

    assert slots == []


# ---------------------------------------------------------------------------
# test_lesson_fits_120min_gap_exactly
# ---------------------------------------------------------------------------

def test_lesson_fits_120min_gap_exactly():
    """A gap of exactly 120 minutes yields exactly 1 slot."""
    appt = _lesson()  # needs exactly 120min

    # Use full-day window but busy events leave exactly a 120min gap at 10:00-12:00
    busy = [(_ws(8, 0), _ws(10, 0)), (_ws(12, 0), _ws(18, 0))]
    work_start = _ws(8, 0)
    work_end = _ws(18, 0)

    slots = compute_available_slots(busy, work_start, work_end, appt)

    # The only gap is 10:00-12:00 = 120min; lesson starts at 10:00+15min = 10:15
    assert slots == [_ws(10, 15)]


# ---------------------------------------------------------------------------
# test_get_busy_intervals_reads_cache
# ---------------------------------------------------------------------------

def test_get_busy_intervals_reads_cache(db_session):
    """get_busy_intervals reads CalendarCache rows for the target date."""
    target = date(2026, 6, 1)
    row1 = CalendarCache(
        event_id="evt-a",
        summary="Lesson A",
        start_dt=datetime(2026, 6, 1, 9, 0),
        end_dt=datetime(2026, 6, 1, 10, 30),
    )
    row2 = CalendarCache(
        event_id="evt-b",
        summary="Lesson B",
        start_dt=datetime(2026, 6, 1, 13, 0),
        end_dt=datetime(2026, 6, 1, 14, 30),
    )
    # Row on a different date — should NOT be returned
    row3 = CalendarCache(
        event_id="evt-c",
        summary="Other Day",
        start_dt=datetime(2026, 6, 2, 9, 0),
        end_dt=datetime(2026, 6, 2, 10, 30),
    )
    db_session.add_all([row1, row2, row3])
    db_session.commit()

    intervals = get_busy_intervals(target, session=db_session)

    assert len(intervals) == 2
    # CalendarCache stores naive UTC; get_busy_intervals returns Copenhagen-local
    # (CEST = UTC+2 on 2026-06-01), so 09:00/13:00 UTC surface as 11:00/15:00 local.
    starts = {iv[0] for iv in intervals}
    assert datetime(2026, 6, 1, 11, 0) in starts
    assert datetime(2026, 6, 1, 15, 0) in starts


# ---------------------------------------------------------------------------
# test_get_busy_intervals_unions_multiple_calendars (FB-02, Phase 999.30)
# ---------------------------------------------------------------------------

def test_get_busy_intervals_unions_multiple_calendars(db_session):
    """FB-02: busy intervals from DIFFERENT calendars are all returned and
    union to suppress overlapping slots — proving slot computation honors all
    calendars, not just the named Lisa calendar.

    Rows are stored naive-UTC (the poller's convention); get_busy_intervals
    converts to Copenhagen local. June 2026 is CEST (UTC+2), so 07:00Z -> 09:00
    local and 08:00Z -> 10:00 local.
    """
    target = date(2026, 6, 1)
    # Two rows simulating two SEPARATE calendars (distinct event_id prefixes).
    cal_a = CalendarCache(
        event_id="cal-A-evt0",
        summary="",  # content-free, as the multi-calendar poller writes
        start_dt=datetime(2026, 6, 1, 7, 0),   # 09:00 local
        end_dt=datetime(2026, 6, 1, 8, 0),     # 10:00 local
    )
    cal_b = CalendarCache(
        event_id="cal-B-evt0",
        summary="",
        start_dt=datetime(2026, 6, 1, 8, 0),   # 10:00 local
        end_dt=datetime(2026, 6, 1, 9, 0),     # 11:00 local
    )
    db_session.add_all([cal_a, cal_b])
    db_session.commit()

    intervals = get_busy_intervals(target, session=db_session)

    # Both calendars' intervals are present (the union, FB-02).
    assert len(intervals) == 2
    local_starts = {iv[0] for iv in intervals}
    assert datetime(2026, 6, 1, 9, 0) in local_starts
    assert datetime(2026, 6, 1, 10, 0) in local_starts

    # Feeding them through slot computation suppresses any slot overlapping the
    # merged 09:00–11:00 busy block — including the window contributed by the
    # SECOND calendar (10:00–11:00), which a single-calendar read would miss.
    work_start = _ws(8, 0)
    work_end = _ws(18, 0)
    slots = compute_available_slots(intervals, work_start, work_end, _lesson())
    busy_lo = datetime(2026, 6, 1, 9, 0)
    busy_hi = datetime(2026, 6, 1, 11, 0)
    assert all(not (busy_lo <= s < busy_hi) for s in slots)


# ---------------------------------------------------------------------------
# test_30min_granularity
# ---------------------------------------------------------------------------

def test_30min_granularity():
    """Consecutive slots are exactly 30 minutes apart."""
    work_start = _ws(8, 0)
    work_end = _ws(10, 0)

    # Use a shorter appointment type so multiple slots fit in a small window
    short_appt = AppointmentType("quick", 15, 0)  # 15min, no buffer
    slots = compute_available_slots([], work_start, work_end, short_appt)

    assert len(slots) >= 2
    for i in range(len(slots) - 1):
        gap = slots[i + 1] - slots[i]
        assert gap == timedelta(minutes=30), f"Gap between slots {i} and {i+1} is {gap}, expected 30min"
