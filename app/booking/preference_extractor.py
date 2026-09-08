"""LLM-based preference extraction from Danish SMS for booking flow."""
import logging
from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo

import anthropic

from app.config import ANTHROPIC_API_KEY, CLASSIFICATION_MODEL

_COPENHAGEN_TZ = ZoneInfo("Europe/Copenhagen")

logger = logging.getLogger(__name__)

_client = None


def _get_client():
    """Lazy-init Anthropic client so import doesn't require API key."""
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    return _client

PREFERENCE_TOOL = {
    "name": "extract_booking_preferences",
    "description": "Extract booking preferences from a student SMS in Danish or English. Extract appointment type, preferred days, time of day, and specific preferred time.",
    "input_schema": {
        "type": "object",
        "properties": {
            "appointment_type": {
                "anyOf": [
                    {"type": "string", "enum": ["lesson", "manoeuvring", "test"]},
                    {"type": "null"}
                ],
                "description": "Type of appointment. 'lesson' for koretime/korelektion, 'manoeuvring' for manoevrebane, 'test' for prove/koreprov. null if not mentioned."
            },
            "days": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Preferred days in Danish (e.g. 'mandag', 'tirsdag') or date strings. Empty array if none mentioned."
            },
            "time_of_day": {
                "anyOf": [
                    {"type": "string", "enum": ["morgen", "formiddag", "eftermiddag", "aften"]},
                    {"type": "null"}
                ],
                "description": "Preferred time of day. morgen=07-09, formiddag=09-12, eftermiddag=12-16, aften=16-18. Infer from preferred_time if not explicit. null if not stated."
            },
            "preferred_time": {
                "anyOf": [
                    {"type": "string"},
                    {"type": "null"}
                ],
                "description": "Specific preferred clock time in HH:MM 24-hour format. Extract from 'kl 14', 'kl. 14:30', 'klokken 9', '2PM', 'halv tre' (=14:30), etc. null if no specific time stated."
            },
            "target_date": {
                "anyOf": [
                    {"type": "string"},
                    {"type": "null"}
                ],
                "description": (
                    "Specific calendar date in YYYY-MM-DD format if the student references "
                    "a single concrete day. Set when the message contains a relative date "
                    "phrase ('i morgen', 'i overmorgen', 'tomorrow', 'this Sunday', 'denne sondag', "
                    "'nu pa sondag', 'om to dage', 'this coming Friday') OR a bare weekday name "
                    "without 'naeste' / 'next' qualifier (treat 'sondag' alone as the SOONEST "
                    "upcoming Sunday, NOT any Sunday). Use the SYSTEM-PROVIDED today's date to "
                    "resolve. 'naeste sondag' / 'next Sunday' = the Sunday AFTER the soonest "
                    "upcoming Sunday. If the student lists multiple days or says 'hvilke dage som helst', "
                    "leave null and use the days array instead. Null when no specific day is implied."
                )
            },
            "earliest_on_day": {
                "type": "boolean",
                "default": False,
                "description": (
                    "True when the student asks for the EARLIEST / FIRST available slot on the "
                    "referenced day. Triggers on Danish 'tidligste', 'tidligste tid', 'sa tidligt "
                    "som muligt', 'forste ledige', 'forst' and English 'earliest', 'first available', "
                    "'as early as possible'. Typically combined with target_date ('tidligste tid pa "
                    "sondag'). False if not stated."
                )
            },
            "latest_on_day": {
                "type": "boolean",
                "default": False,
                "description": (
                    "True when the student asks for the LATEST / LAST available slot on the "
                    "referenced day. Triggers on Danish 'seneste', 'seneste tid', 'sa sent som "
                    "muligt', 'sidste ledige', 'sidst' and English 'latest', 'last available', "
                    "'as late as possible'. Typically combined with target_date ('seneste tid i "
                    "morgen'). False if not stated."
                )
            },
            "date_range_start": {
                "anyOf": [
                    {"type": "string"},
                    {"type": "null"}
                ],
                "description": (
                    "Inclusive lower bound of a multi-day window in YYYY-MM-DD format. Set when the "
                    "student references a range or open window: 'i naeste uge' (Monday of next week), "
                    "'i denne uge' (today, since past days can't be booked), 'uge N' (Monday of ISO "
                    "week N), 'om N uger' (Monday of that week), 'efter den X' (the day AFTER X), "
                    "'mellem X og Y' (X), 'foer den X' (today). Both date_range_start AND "
                    "date_range_end must be set together. Leave null when target_date is set or "
                    "when no range is implied."
                )
            },
            "date_range_end": {
                "anyOf": [
                    {"type": "string"},
                    {"type": "null"}
                ],
                "description": (
                    "Inclusive upper bound of a multi-day window in YYYY-MM-DD format. Set when the "
                    "student references a range or open window: 'i naeste uge' (Sunday of next "
                    "week), 'i denne uge' (Sunday of this week), 'uge N' (Sunday of ISO week N), "
                    "'om N uger' (Sunday of that week), 'foer den X' (the day BEFORE X — strictly "
                    "before), 'efter den X' (X + 14 days), 'mellem X og Y' (Y). Both bounds must be "
                    "set together. Leave null when target_date is set or when no range is implied."
                )
            }
        },
        "required": ["appointment_type", "days", "time_of_day", "preferred_time", "target_date"]
    }
}

EXTRACTION_SYSTEM_PROMPT_TEMPLATE = (
    "You extract booking preferences from student SMS messages for a Danish driving school. "
    "Today's date is {today} ({weekday}) in Europe/Copenhagen. Use this to resolve relative dates. "
    "Parse the student's free-text message and extract: appointment type, preferred days, time of day, specific preferred time, and target_date. "
    "Danish day names: mandag, tirsdag, onsdag, torsdag, fredag, lordag, sondag. "
    "Time ranges: morgen (07-09), formiddag (09-12), eftermiddag (12-16), aften (16-18). "
    "Appointment types: lesson (koretime/korelektion), manoeuvring (manoevrebane), test (prove/koreprov). "
    "For preferred_time: convert to HH:MM 24-hour format. 'kl 14' -> '14:00', 'halv tre' -> '14:30', '9' or 'kl 9' -> '09:00'. "
    "If a specific time is given, also infer time_of_day from it (e.g. 14:00 -> eftermiddag). "
    "TARGET_DATE rules (critical): "
    "(a) Set target_date to the SOONEST matching date when the message references a single specific day. "
    "(b) A bare weekday name ('sondag', 'fredag') without 'naeste' / 'next' means the SOONEST upcoming "
    "occurrence of that weekday from today's date - never multiple weeks out. "
    "(c) 'denne sondag', 'nu pa sondag', 'this Sunday', 'this coming Sunday' all mean the SOONEST upcoming Sunday. "
    "(d) 'naeste sondag', 'next Sunday' means the Sunday AFTER the soonest upcoming one (i.e. one week later). "
    "(e) 'i morgen' / 'tomorrow' = today + 1 day. 'i overmorgen' = today + 2 days. "
    "(f) When target_date is set, also include the resolved weekday name in the days array for back-compat. "
    "(g) If the student names multiple days, lists alternatives, or asks open-endedly (e.g. 'hvilke dage som helst', "
    "'tirsdag eller onsdag'), leave target_date null and only fill the days array. "
    "EARLIEST/LATEST rules: "
    "Set earliest_on_day=true when the message asks for the FIRST / EARLIEST slot ('tidligste', 'tidligste tid', "
    "'sa tidligt som muligt', 'forste ledige', 'earliest', 'as early as possible'). "
    "Set latest_on_day=true when the message asks for the LAST / LATEST slot ('seneste', 'seneste tid', "
    "'sa sent som muligt', 'sidste ledige', 'latest', 'as late as possible'). "
    "These flags are usually paired with target_date ('tidligste tid pa sondag' = target_date + earliest_on_day=true). "
    "Both flags default to false when not stated. "
    "DATE RANGE rules: "
    "Set BOTH date_range_start AND date_range_end (or neither) for multi-day windows: "
    "(a) 'i naeste uge' / 'next week' = Monday..Sunday of the week AFTER today's week. "
    "(b) 'i denne uge' / 'this week' = today..Sunday of today's week (never set start in the past). "
    "(c) 'uge N' / 'week N' = Monday..Sunday of ISO week N of the current year (or next year if N is in the past). "
    "(d) 'om N uger' / 'in N weeks' = Monday..Sunday of the week N weeks from today's week. "
    "(e) 'foer den X' / 'before X' = today..(X - 1 day). The bound is STRICT — slots ON X are excluded. "
    "(f) 'efter den X' / 'after X' = (X + 1 day)..(X + 14 days). "
    "(g) 'mellem X og Y' / 'between X and Y' = X..Y (inclusive both ends). "
    "Use today's date and weekday (provided above) to resolve. ISO weeks start on Monday. "
    "When date_range_* is set, leave target_date null. When target_date is set, leave date_range_* null. "
    "When neither a range nor a target_date applies, leave both null and use the days array. "
    "If a field is not mentioned, return null (for appointment_type, time_of_day, preferred_time, target_date, "
    "date_range_start, date_range_end) or empty array (for days)."
)


def _today_copenhagen() -> date:
    """Return today's date in Europe/Copenhagen (the school's local tz)."""
    return datetime.now(_COPENHAGEN_TZ).date()


_DA_WEEKDAYS = ["mandag", "tirsdag", "onsdag", "torsdag", "fredag", "lordag", "sondag"]


@dataclass
class BookingPreferences:
    appointment_type: str | None  # "lesson" | "manoeuvring" | "test" | None
    days: list[str]               # Danish day names or date strings
    time_of_day: str | None       # "morgen" | "formiddag" | "eftermiddag" | "aften" | None
    preferred_time: str | None    # specific clock time "HH:MM" | None
    target_date: str | None = None  # ISO YYYY-MM-DD for single-day requests ("denne sondag")
    earliest_on_day: bool = False   # "tidligste tid" — pick first slot of the day
    latest_on_day: bool = False     # "seneste tid" — pick last slot of the day
    date_range_start: str | None = None  # ISO YYYY-MM-DD inclusive lower bound ("i naeste uge")
    date_range_end: str | None = None    # ISO YYYY-MM-DD inclusive upper bound


def extract_preferences(message: str) -> BookingPreferences:
    """Extract booking preferences from a student SMS using Claude API tool_use.

    Today's Copenhagen date is injected into the system prompt so the LLM can
    resolve relative date phrases ('denne sondag', 'i morgen', 'naeste fredag')
    to an absolute YYYY-MM-DD in target_date.

    Returns BookingPreferences with None/empty defaults on any failure.
    """
    today = _today_copenhagen()
    system_prompt = EXTRACTION_SYSTEM_PROMPT_TEMPLATE.format(
        today=today.isoformat(),
        weekday=_DA_WEEKDAYS[today.weekday()],
    )

    try:
        response = _get_client().messages.create(
            model=CLASSIFICATION_MODEL,
            max_tokens=256,
            system=system_prompt,
            messages=[{"role": "user", "content": message}],
            tools=[PREFERENCE_TOOL],
            tool_choice={"type": "tool", "name": "extract_booking_preferences"},
        )

        for block in response.content:
            if block.type == "tool_use" and block.name == "extract_booking_preferences":
                return BookingPreferences(
                    appointment_type=block.input.get("appointment_type"),
                    days=block.input.get("days", []),
                    time_of_day=block.input.get("time_of_day"),
                    preferred_time=block.input.get("preferred_time"),
                    target_date=block.input.get("target_date"),
                    earliest_on_day=bool(block.input.get("earliest_on_day", False)),
                    latest_on_day=bool(block.input.get("latest_on_day", False)),
                    date_range_start=block.input.get("date_range_start"),
                    date_range_end=block.input.get("date_range_end"),
                )

        logger.warning("No tool_use block in preference extraction response")
        return BookingPreferences(appointment_type=None, days=[], time_of_day=None, preferred_time=None)

    except Exception:
        logger.exception("Preference extraction API call failed")
        return BookingPreferences(appointment_type=None, days=[], time_of_day=None, preferred_time=None)
