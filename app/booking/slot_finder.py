"""Multi-day slot search with time-of-day filtering for booking flow."""
import logging
from datetime import date, datetime, time, timedelta

from app.calendar import slot_engine
from app.calendar.poller import refresh_if_stale
from app.calendar.slot_engine import get_slots_for_date, load_appointment_types

logger = logging.getLogger(__name__)


def _today_copenhagen() -> date:
    """Today's date in Europe/Copenhagen — the school's local day. Patchable seam.

    Deliberately NOT ``date.today()``, which reads the *system* timezone: Railway
    sets no ``TZ``, so the container runs UTC. Around Copenhagen midnight the two
    disagree by a whole day — at 00:30 Copenhagen in summer it is still 22:30 UTC
    *yesterday*. Every day-walk below is anchored on this date, while the
    front-edge filter inside ``get_slots_for_date`` works in Copenhagen local
    time, so a UTC anchor puts the two out of step: the walk can surface a slot
    on a day the student reads as different from the one Lisa means, or skip a
    genuinely bookable day entirely. Wrong-day offers are exactly the kind of
    error a booking assistant cannot afford.

    Derived from ``slot_engine._now_local()`` rather than reading the clock
    again, so the day-walk and the front-edge filter it feeds cannot disagree.
    Production behaviour is identical either way — both are Copenhagen-local
    now — but two independent reads mean a test can freeze one and not the
    other, and then exercises a state that cannot occur in production. Since
    the whole point of this function is that these two must stay in step,
    sharing the source is the enforcement. Accessed via the module (not a
    ``from`` import) so patching ``slot_engine._now_local`` reaches here too.
    Patching this function directly still overrides, for tests that want the
    day-walk pinned independently.
    """
    return slot_engine._now_local().date()


# Per D-03: Time-of-day ranges
TIME_OF_DAY_RANGES: dict[str, tuple[time, time]] = {
    "morgen":       (time(7,  0), time(9,  0)),
    "formiddag":    (time(9,  0), time(12, 0)),
    "eftermiddag":  (time(12, 0), time(16, 0)),
    "aften":        (time(16, 0), time(18, 0)),
}

# Danish day name to weekday index (Monday=0)
DANISH_DAY_NAMES: dict[str, int] = {
    "mandag": 0, "tirsdag": 1, "onsdag": 2, "torsdag": 3,
    "fredag": 4, "lordag": 5, "sondag": 6,
    "lørdag": 5, "søndag": 6,
}


def find_earliest_slot(
    appt_type_name: str,
    max_days_ahead: int = 14,
    time_of_day: str | None = None,
    preferred_time: str | None = None,
    session=None,
) -> datetime | None:
    """Return the earliest available slot across the next max_days_ahead days.

    Args:
        appt_type_name: Key into APPOINTMENT_TYPES (e.g. "lesson").
        max_days_ahead: How many days forward to search.
        time_of_day: Optional time-of-day filter ("morgen"|"formiddag"|"eftermiddag"|"aften").
        session: Optional SQLAlchemy session (for testing).

    Returns:
        The earliest slot start datetime (naive UTC), or None if nothing found.
    """
    slots = find_earliest_slots(
        appt_type_name,
        n=1,
        max_days_ahead=max_days_ahead,
        time_of_day=time_of_day,
        preferred_time=preferred_time,
        session=session,
    )
    return slots[0] if slots else None


def find_earliest_slots(
    appt_type_name: str,
    n: int = 3,
    max_days_ahead: int = 14,
    time_of_day: str | None = None,
    preferred_time: str | None = None,
    min_spacing_minutes: int = 0,
    session=None,
) -> list[datetime]:
    """Return up to N earliest available slots in chronological order.

    Walks forward day-by-day, accumulating matching slots, until N are
    collected or max_days_ahead is exhausted. Used by the multi-slot offer
    path so the student can pick from a numbered list in one SMS instead
    of one round-trip per "no".

    When min_spacing_minutes > 0, slots are greedy-picked so each pick is
    at least that many minutes after the previous pick's start. Used to
    give a wider choice (e.g. 08:00 / 09:00 / 10:00) instead of three
    back-to-back slots (08:00 / 08:30 / 09:00). If the time-of-day band
    can't fit N spaced picks, returns fewer slots.

    Returns an empty list when nothing matches; the single-slot path uses
    n=1 + first-or-None at the call site.
    """
    appt_types = load_appointment_types()
    appt_type = appt_types.get(appt_type_name)
    if not appt_type:
        logger.error("Unknown appointment type: %s", appt_type_name)
        return []

    if session is None:
        refresh_if_stale()

    collected: list[datetime] = []
    today = _today_copenhagen()
    for delta in range(1, max_days_ahead + 1):
        target = today + timedelta(days=delta)
        slots = get_slots_for_date(target, appt_type, session=session)
        if time_of_day:
            slots = filter_slots_by_time_of_day(slots, time_of_day)
        if preferred_time:
            slots = filter_slots_by_preferred_time(slots, preferred_time)
        for s in slots:
            if _too_close(s, collected, min_spacing_minutes):
                continue
            collected.append(s)
            if len(collected) >= n:
                return collected
    return collected


def _too_close(
    candidate: datetime,
    picked: list[datetime],
    min_spacing_minutes: int,
) -> bool:
    """Return True if candidate is within min_spacing_minutes of the most recent pick.

    No-op when min_spacing_minutes <= 0 or when nothing has been picked yet.
    Spacing is measured from the LAST pick only (greedy walk) — picks are
    chronologically ordered so this is sufficient.
    """
    if min_spacing_minutes <= 0 or not picked:
        return False
    delta_s = (candidate - picked[-1]).total_seconds()
    return delta_s < min_spacing_minutes * 60


def find_slots_for_preferences(
    appt_type_name: str,
    preferred_days: list[str],
    time_of_day: str | None = None,
    preferred_time: str | None = None,
    weeks_ahead: int = 3,
    min_spacing_minutes: int = 0,
    session=None,
) -> list[datetime]:
    """Find slots matching day + time-of-day preferences.

    Args:
        appt_type_name: Key into APPOINTMENT_TYPES.
        preferred_days: Danish day names (e.g. ["tirsdag", "onsdag"]).
        time_of_day: "morgen" | "formiddag" | "eftermiddag" | "aften" | None.
        weeks_ahead: How many weeks to search.
        session: Optional SQLAlchemy session.

    Returns:
        List of matching slot datetimes (naive UTC), sorted chronologically.
    """
    appt_types = load_appointment_types()
    appt_type = appt_types.get(appt_type_name)
    if not appt_type:
        logger.error("Unknown appointment type: %s", appt_type_name)
        return []

    if session is None:
        refresh_if_stale()

    # Convert Danish day names to weekday indices
    target_weekdays = set()
    for day_name in preferred_days:
        weekday = DANISH_DAY_NAMES.get(day_name.lower())
        if weekday is not None:
            target_weekdays.add(weekday)

    if not target_weekdays:
        if time_of_day or preferred_time:
            # Time-only refinement ("lidt senere tider", "efter skoletid") —
            # student narrowed time without restating a day. Search all weekdays
            # and let the time filters do the work; non-business days return
            # empty from get_slots_for_date so this is safe.
            target_weekdays = set(range(7))
        else:
            logger.warning("No valid day names in preferences: %s", preferred_days)
            return []

    today = _today_copenhagen()
    matching_slots: list[datetime] = []

    for delta in range(1, weeks_ahead * 7 + 1):
        target = today + timedelta(days=delta)
        if target.weekday() not in target_weekdays:
            continue

        slots = get_slots_for_date(target, appt_type, session=session)
        if time_of_day:
            slots = filter_slots_by_time_of_day(slots, time_of_day)
        if preferred_time:
            slots = filter_slots_by_preferred_time(slots, preferred_time)
        for s in slots:
            if _too_close(s, matching_slots, min_spacing_minutes):
                continue
            matching_slots.append(s)

    return matching_slots


def filter_slots_by_time_of_day(
    slots: list[datetime],
    time_of_day: str,
) -> list[datetime]:
    """Filter slot datetimes to those within a time-of-day range.

    Args:
        slots: List of naive UTC datetimes.
        time_of_day: Key into TIME_OF_DAY_RANGES.

    Returns:
        Filtered list of slots within the range.
    """
    if time_of_day not in TIME_OF_DAY_RANGES:
        return slots
    start, end = TIME_OF_DAY_RANGES[time_of_day]
    return [s for s in slots if start <= s.time() < end]


PREFERRED_TIME_TOLERANCE_MIN = 15


def filter_slots_by_preferred_time(
    slots: list[datetime],
    preferred_time: str,
    tolerance_minutes: int = PREFERRED_TIME_TOLERANCE_MIN,
) -> list[datetime]:
    """Filter slots to those at/after a preferred time (HH:MM), with a small
    grace window BEFORE it so a slot up to ``tolerance_minutes`` early still
    qualifies (e.g. 11:45 counts for a 12:00 request when 12:00 sharp collides
    with the inter-lesson buffer).

    Soft filter: if nothing qualifies, returns the original list so the caller
    always has something to offer. The caller is responsible for acknowledging
    (via ``preferred_time_satisfied``) when the exact time couldn't be honored.
    """
    try:
        h, m = map(int, preferred_time.split(":"))
        threshold = h * 60 + m - tolerance_minutes
    except (ValueError, AttributeError):
        return slots
    filtered = [s for s in slots if (s.hour * 60 + s.minute) >= threshold]
    return filtered if filtered else slots


def preferred_time_satisfied(
    slots: list[datetime],
    preferred_time: str,
    tolerance_minutes: int = PREFERRED_TIME_TOLERANCE_MIN,
) -> bool:
    """True if any slot starts within ±tolerance of preferred_time.

    Used by the offer layer to decide whether to acknowledge that the student's
    requested clock time wasn't available ("Kl. 12 er ikke ledig, men …").
    Unparseable preferred_time → True (don't claim unavailable).
    """
    try:
        h, m = map(int, preferred_time.split(":"))
        pref = h * 60 + m
    except (ValueError, AttributeError):
        return True
    return any(
        abs((s.hour * 60 + s.minute) - pref) <= tolerance_minutes for s in slots
    )


def find_slots_for_date(
    appt_type_name: str,
    target_date: date,
    time_of_day: str | None = None,
    preferred_time: str | None = None,
    earliest_on_day: bool = False,
    latest_on_day: bool = False,
    min_spacing_minutes: int = 0,
    session=None,
) -> list[datetime]:
    """Find slots on a single specific date.

    Used when the student references a single concrete day ("denne sondag",
    "i morgen") and the LLM extractor returned a resolved YYYY-MM-DD via
    BookingPreferences.target_date. Unlike find_slots_for_preferences, this
    does NOT fall through to subsequent occurrences of the same weekday when
    the requested day has no matching slots — silent fall-through caused
    "denne sondag" to be answered with next Sunday's slot.

    When earliest_on_day is set, returns only the first remaining slot of
    that day after time-of-day / preferred-time filtering. latest_on_day
    returns only the last. earliest wins if both are set.

    Returns an empty list when the target date has no matching slots; the
    caller is responsible for surfacing a no-slots-that-day message and
    suggesting an alternative.
    """
    appt_types = load_appointment_types()
    appt_type = appt_types.get(appt_type_name)
    if not appt_type:
        logger.error("Unknown appointment type: %s", appt_type_name)
        return []

    if session is None:
        refresh_if_stale()

    slots = get_slots_for_date(target_date, appt_type, session=session)
    if time_of_day:
        slots = filter_slots_by_time_of_day(slots, time_of_day)
    if preferred_time:
        slots = filter_slots_by_preferred_time(slots, preferred_time)
    if earliest_on_day and slots:
        return [slots[0]]
    if latest_on_day and slots:
        return [slots[-1]]
    if min_spacing_minutes > 0:
        spaced: list[datetime] = []
        for s in slots:
            if _too_close(s, spaced, min_spacing_minutes):
                continue
            spaced.append(s)
        return spaced
    return slots


def find_slots_in_range(
    appt_type_name: str,
    start: date,
    end: date,
    time_of_day: str | None = None,
    preferred_time: str | None = None,
    max_slots: int = 3,
    min_spacing_minutes: int = 0,
    session=None,
) -> list[datetime]:
    """Find slots within an inclusive [start, end] date range.

    Used when the student references a multi-day window ("i naeste uge",
    "foer den 20.", "mellem den 10. og 15.") and the LLM extractor returned
    `date_range_start` / `date_range_end`. Bounded loop — never falls through
    past `end`, so "foer den 20." cannot return a slot on or after the 20th.

    Past dates in `start` are clamped to today + 1 (we don't search history).
    Returns up to `max_slots` slots in chronological order across the window.
    Empty list when nothing matches; caller surfaces the explicit fallback.
    """
    appt_types = load_appointment_types()
    appt_type = appt_types.get(appt_type_name)
    if not appt_type:
        logger.error("Unknown appointment type: %s", appt_type_name)
        return []

    if session is None:
        refresh_if_stale()

    today = _today_copenhagen()
    cursor = max(start, today + timedelta(days=1))
    if cursor > end:
        return []

    collected: list[datetime] = []
    while cursor <= end:
        slots = get_slots_for_date(cursor, appt_type, session=session)
        if time_of_day:
            slots = filter_slots_by_time_of_day(slots, time_of_day)
        if preferred_time:
            slots = filter_slots_by_preferred_time(slots, preferred_time)
        for s in slots:
            if _too_close(s, collected, min_spacing_minutes):
                continue
            collected.append(s)
            if len(collected) >= max_slots:
                return collected
        cursor += timedelta(days=1)
    return collected


def find_nearest_alternative(
    appt_type_name: str,
    max_days_ahead: int = 14,
    session=None,
) -> datetime | None:
    """Find the nearest available slot regardless of day/time preferences.

    Used per D-07 when no slots match the student's preference.
    Delegates to find_earliest_slot.
    """
    return find_earliest_slot(appt_type_name, max_days_ahead, session=session)
