"""Multi-step booking flow handler for SMS booking conversations."""
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.booking.preference_extractor import extract_preferences
from app.booking.slot_finder import (
    find_earliest_slot,
    find_earliest_slots,
    find_nearest_alternative,
    find_slots_for_date,
    find_slots_for_preferences,
    find_slots_in_range,
    preferred_time_satisfied,
)

from app.calendar.client import build_calendar_service, create_event
from app.calendar.slot_engine import get_busy_intervals, load_appointment_types
from app.config import (
    GCAL_CALENDAR_ID,
    LEKTIONSPLAN_LABELS,
    MANUAL_BOOKING_LESSONS,
    MAX_BOOKINGS_PER_DAY,
    MAX_OUTSTANDING_BOOKINGS,
    OWNER_PHONE,
)
from app.conversation.mid_flow_faq import (
    MAX_MID_FLOW_FAQ_PASSTHROUGHS,
    increment_faq_count,
    reset_faq_count,
    try_mid_flow_faq,
)
from app.conversation.affirmation import is_affirmation
from app.conversation.state import ConversationState
from app.escalation.forward import forward_to_owner
from app.lead.capture import (
    get_student_lesson,
    get_student_name,
    set_student_lesson,
    upsert_student_name,
)
from app.models import Booking, Conversation, Message
from app.sms.sender import cc_school_owner, send_sms

logger = logging.getLogger(__name__)

COPENHAGEN_TZ = ZoneInfo("Europe/Copenhagen")

# Maximum slots offered in a single SMS. SMS budget: ~25 chars per formatted
# slot + "N) " prefix + " " separator + ~22 chars suffix = ~95 chars at 3
# slots. Comfortably under 160. Going higher pushes into split territory.
MAX_OFFER_SLOTS = 3


def _now_local() -> datetime:
    """Current wall-clock time as a naive Copenhagen-local datetime.

    Patchable seam for the confirm-time past-slot guard so the time-travelling
    test suite (which mocks slot finders with fixed historical dates) can freeze
    the guard's clock without touching other now-usages.
    """
    return datetime.now(COPENHAGEN_TZ).replace(tzinfo=None)


# Appointment type display names for SMS (Danish)
APPT_TYPE_NAMES_DA = {
    "lesson": "køretime",
    "manoeuvring": "manøvrebane",
    "test": "køreprøve",
}
APPT_TYPE_NAMES_EN = {
    "lesson": "driving lesson",
    "manoeuvring": "manoeuvring course",
    "test": "driving test",
}
# Plurals used when an offer mentions the type ("Ledige køretimer: …").
# Only Danish needs explicit pluralization; the English plural is
# regular (-s) but we keep the map for consistency.
APPT_TYPE_PLURAL_DA = {
    "lesson": "køretimer",
    "manoeuvring": "manøvrebaner",
    "test": "køreprøver",
}
APPT_TYPE_PLURAL_EN = {
    "lesson": "driving lessons",
    "manoeuvring": "manoeuvring courses",
    "test": "driving tests",
}


def start_booking_flow(
    db: Session,
    conversation: Conversation,
    body: str,
    language: str,
) -> tuple[str, ConversationState]:
    """Entry point: book_lesson intent detected in IDLE state.

    Per D-06: extract appointment_type from message if stated, otherwise ask.
    Per D-01: if type is known, immediately offer earliest slot.

    Name pre-flight: if no Lead.name on file for this phone, ask once before
    showing slots so the instructor's notification carries a real name.
    """
    # Darkness driving (mørkekørsel / lektion 44) is not auto-bookable — deflect an
    # explicit request to manual arrangement with the instructor before anything else.
    if _DARKNESS_RE.search(body):
        return _darkness_manual_reply(db, conversation, language)

    # Hard outstanding-bookings cap (anti calendar-spam during the priority
    # pilot). Checked before anything else so a capped student is told up front
    # instead of after picking a slot. No-op when MAX_OUTSTANDING_BOOKINGS == 0.
    if MAX_OUTSTANDING_BOOKINGS > 0 and _outstanding_booking_count(
        db, conversation.phone_number
    ) >= MAX_OUTSTANDING_BOOKINGS:
        if language == "en":
            return (
                f"You already have {MAX_OUTSTANDING_BOOKINGS} lessons booked, which is the "
                f"limit for now. You can book another after your next lesson.",
                ConversationState.IDLE,
            )
        return (
            f"Du har allerede {MAX_OUTSTANDING_BOOKINGS} køretimer booket, hvilket er loftet "
            f"indtil videre. Du kan booke en til efter din næste køretime.",
            ConversationState.IDLE,
        )

    prefs = extract_preferences(body)

    # One-per-day cap, surfaced up front: if the student names a day they already
    # have a booking on, deny now rather than offering slots we'd refuse at the
    # confirm step (which felt like a bait-and-switch). The confirm-time guard in
    # _confirm_booking remains the hard backstop for the no-day-specified path.
    if MAX_BOOKINGS_PER_DAY > 0 and prefs.target_date:
        try:
            requested_date = datetime.fromisoformat(prefs.target_date).date()
        except (ValueError, TypeError):
            requested_date = None
        if requested_date and _booking_count_on_date(
            db, conversation.phone_number, requested_date
        ) >= MAX_BOOKINGS_PER_DAY:
            if language == "en":
                return (
                    "You already have a lesson booked that day — only one per day for now. "
                    "Pick another day and I'll find a time.",
                    ConversationState.IDLE,
                )
            return (
                "Du har allerede en køretime den dag — der kan kun bookes én pr. dag indtil "
                "videre. Vælg en anden dag, så finder jeg en tid.",
                ConversationState.IDLE,
            )

    context: dict = {"booking": {}}

    # Stash any preferences extracted from the entry message so we can resume
    # the original flow once name capture (if needed) completes.
    if prefs.appointment_type:
        context["booking"]["appointment_type"] = prefs.appointment_type
    if prefs.time_of_day:
        context["booking"]["time_of_day"] = prefs.time_of_day
    if prefs.preferred_time:
        context["booking"]["preferred_time"] = prefs.preferred_time
    if prefs.target_date:
        context["booking"]["target_date"] = prefs.target_date
    if prefs.earliest_on_day:
        context["booking"]["earliest_on_day"] = True
    if prefs.latest_on_day:
        context["booking"]["latest_on_day"] = True
    if prefs.date_range_start and prefs.date_range_end:
        context["booking"]["date_range_start"] = prefs.date_range_start
        context["booking"]["date_range_end"] = prefs.date_range_end

    if get_student_name(db, conversation.phone_number) is None:
        _save_context(conversation, context)
        if language == "en":
            return (
                "What's your full name? (first-time booking only)",
                ConversationState.BOOKING_INTAKE_NAME,
            )
        return (
            "Hvad er dit fulde navn? (kun ved første booking)",
            ConversationState.BOOKING_INTAKE_NAME,
        )

    if prefs.appointment_type:
        _save_context(conversation, context)
        return _offer_earliest_slot(db, conversation, language, context)

    # Phase 999.8: returning-student type default. If the phone has a clean
    # single-type booking history in the last 90 days, skip the "hvad vil
    # du booke?" question and offer that type directly. Mixed history or no
    # history → fall through to the question (current behaviour).
    default_type = _default_returning_type(db, conversation.phone_number)
    if default_type:
        context["booking"]["appointment_type"] = default_type
        # Flag so the first offer SMS mentions the type ("Næste ledige
        # køretime er ...") — the student didn't pick the type explicitly,
        # so naming it gives them a chance to override before booking.
        context["booking"]["type_defaulted"] = True
        _save_context(conversation, context)
        return _offer_earliest_slot(db, conversation, language, context)

    # Type not stated — ask (per D-06)
    _save_context(conversation, context)
    if language == "en":
        return (
            "What would you like to book? Driving lesson, manoeuvring course, or driving test?",
            ConversationState.BOOKING_SELECT_TYPE,
        )
    return (
        "Hvad vil du booke? Køretime, manøvrebane eller prøve?",
        ConversationState.BOOKING_SELECT_TYPE,
    )


def handle_booking_status(
    db: Session,
    conversation: Conversation,
    language: str,
) -> str:
    """Answer "when is my next køretime?" from the Booking table (read-only).

    Looks up the student's future confirmed bookings and reports the soonest one
    (plus a count of any others). Never invents data — if there are none, it says
    so and points to the booking phrase. Booking.start_dt is naive Copenhagen
    local, so compare against _now_local() (same frame).
    """
    now = _now_local()
    rows = (
        db.query(Booking)
        .filter(
            Booking.phone_number == conversation.phone_number,
            Booking.status == "confirmed",
            Booking.start_dt > now,
        )
        .order_by(Booking.start_dt.asc())
        .all()
    )

    if not rows:
        if language == "en":
            return (
                "You have no upcoming lessons booked. To book one, just write "
                "\"I'd like to book a lesson\" and we'll find a time."
            )
        return (
            "Du har ingen kommende køretimer booket. Skriv \"Jeg vil gerne booke "
            "en køretime\", så finder vi en tid."
        )

    nxt = rows[0]
    formatted = _format_slot(nxt.start_dt)
    extra = len(rows) - 1

    if language == "en":
        appt = APPT_TYPE_NAMES_EN.get(nxt.appointment_type, nxt.appointment_type)
        msg = f"Your next {appt} is {formatted}."
        if extra:
            msg += f" You have {extra} more booked after that."
        return msg

    appt = APPT_TYPE_NAMES_DA.get(nxt.appointment_type, nxt.appointment_type)
    msg = f"Din næste {appt} er {formatted}."
    if extra:
        msg += f" Du har {extra} mere booket derudover."
    return msg


def _outstanding_booking_count(db: Session, phone_number: str) -> int:
    """Count a student's FUTURE confirmed bookings (any type).

    Used by the MAX_OUTSTANDING_BOOKINGS cap. Booking.start_dt is naive UTC, so
    compare against naive-UTC now. Cancelled / reschedule_pending rows are
    excluded by status='confirmed'; past lessons don't count toward the cap.
    """
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    return (
        db.query(Booking)
        .filter(
            Booking.phone_number == phone_number,
            Booking.status == "confirmed",
            Booking.start_dt > now,
        )
        .count()
    )


def _booking_count_on_date(db: Session, phone_number: str, target_date) -> int:
    """Count a student's confirmed bookings on a single calendar day.

    Used by the MAX_BOOKINGS_PER_DAY cap. Booking.start_dt is naive Copenhagen
    local (same frame as the offered slot), so the date comparison is done in
    Python to stay dialect-agnostic (SQLite dev / Postgres prod) and avoid
    midnight-timezone edges. No future filter: an earlier slot already taken the
    same day still counts toward the one-per-day limit. Cancelled /
    reschedule_pending rows are excluded by status='confirmed' (so rescheduling
    within the same day is not blocked by the booking being rescheduled).
    """
    rows = (
        db.query(Booking.start_dt)
        .filter(
            Booking.phone_number == phone_number,
            Booking.status == "confirmed",
        )
        .all()
    )
    return sum(1 for (start_dt,) in rows if start_dt.date() == target_date)


def _default_returning_type(db: Session, phone_number: str) -> str | None:
    """Return the consistent appointment type from the student's recent
    confirmed bookings, or None if there's no signal.

    Used to skip the "hvad vil du booke?" question for repeat students who
    have only ever booked one type. Window is 90 days (created_at) so the
    default expires if the student goes quiet long enough that their
    preference may have changed.

    Returns None when the student is brand-new, when their recent history
    spans multiple types (mixed → still ask), or when they've only ever
    cancelled (cancelled rows are filtered out by status='confirmed').
    """
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=90)
    rows = (
        db.query(Booking.appointment_type)
        .filter(
            Booking.phone_number == phone_number,
            Booking.status == "confirmed",
            Booking.created_at >= cutoff,
        )
        .distinct()
        .all()
    )
    if len(rows) == 1:
        return rows[0][0]
    return None


def handle_booking_step(
    db: Session,
    conversation: Conversation,
    body: str,
    language: str,
) -> tuple[str, ConversationState]:
    """Handle one step of the booking flow. Returns (response_text, new_state)."""
    state = ConversationState(conversation.state)
    context = _get_context(conversation)

    if state == ConversationState.BOOKING_INTAKE_NAME:
        return _handle_intake_name(db, conversation, body, language, context)
    if state == ConversationState.BOOKING_INTAKE_LESSON:
        return _handle_intake_lesson(db, conversation, body, language, context)
    if state == ConversationState.BOOKING_CONFIRM_LESSON:
        return _handle_confirm_lesson(db, conversation, body, language, context)
    if state == ConversationState.BOOKING_SELECT_TYPE:
        return _handle_select_type(db, conversation, body, language, context)
    if state == ConversationState.BOOKING_OFFER_SLOT:
        return _handle_offer_reply(db, conversation, body, language, context)
    if state == ConversationState.BOOKING_SELECT_DAY:
        return _handle_select_day(db, conversation, body, language, context)
    if state == ConversationState.BOOKING_SELECT_TIME:
        return _handle_select_time(db, conversation, body, language, context)

    # Shouldn't happen, but reset gracefully
    return _fallback(language), ConversationState.IDLE


# Keywords that signal the student's name-intake reply also contains booking
# content (specific time, day, type). Used to split combined replies like
# "Dione, jeg vil gerne booke køretime i morgen kl 11" into name + booking
# continuation so the time/date doesn't get swallowed as part of the name.
_NAME_BOOKING_SPLIT_RE = re.compile(
    r"\b(jeg vil|vil gerne|vil have|vil booke|kan jeg|kan vi|booke|book a|"
    r"køretime|koretime|manøvrebane|manoevrebane|køreprøve|koreprov|prøve|prov|"
    r"i morgen|i overmorgen|i dag|tomorrow|today|"
    r"kl\.?\s*\d|klokken|"
    r"mandag|tirsdag|onsdag|torsdag|fredag|lørdag|lordag|søndag|sondag|"
    r"monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
    r"næste uge|naeste uge|denne uge|next week|this week)\b",
    re.IGNORECASE,
)


def _split_name_and_continuation(body: str) -> tuple[str, str | None]:
    """Split a name-intake reply into (name_candidate, continuation).

    Handles three patterns:
    1. Comma split: "Dione, jeg vil gerne booke kl 11" → ("Dione", "jeg vil...")
    2. Booking-keyword split: "Dione jeg vil booke kl 11" → ("Dione", "jeg vil...")
    3. Plain name: "Dione Holm" → ("Dione Holm", None)

    Returns (name_candidate, None) when no continuation is detected.
    """
    body = body.strip()

    # 1. Comma split — strongest signal of "name, then more"
    if "," in body:
        head, _, tail = body.partition(",")
        tail = tail.strip()
        if tail:
            return head.strip(), tail

    # 2. Booking-keyword split — find first match and treat prefix as name
    m = _NAME_BOOKING_SPLIT_RE.search(body)
    if m and m.start() > 0:
        prefix = body[: m.start()].strip()
        suffix = body[m.start():].strip()
        if prefix:
            return prefix, suffix

    return body, None


def _handle_intake_name(
    db: Session,
    conversation: Conversation,
    body: str,
    language: str,
    context: dict,
) -> tuple[str, ConversationState]:
    """Capture the student's full name on first booking, then resume the flow.

    If the reply combines a name with booking content ("Dione, jeg vil booke
    kl 11"), the name is extracted and the booking continuation is re-passed
    through preference extraction so time/date/type don't get lost.
    """
    name, continuation = _split_name_and_continuation(body)
    is_valid = 2 <= len(name) <= 40 and any(c.isalpha() for c in name)
    booking = context.setdefault("booking", {})

    if not is_valid:
        attempts = booking.get("name_attempts", 0) + 1
        booking["name_attempts"] = attempts
        if attempts >= 3:
            _save_context(conversation, {})
            return (
                forward_to_owner(db, conversation, body, reason="booking_name_3x", language=language),
                ConversationState.IDLE,
            )
        _save_context(conversation, context)
        if language == "en":
            return (
                "Please write your full name (e.g. 'Anders Nielsen').",
                ConversationState.BOOKING_INTAKE_NAME,
            )
        return (
            "Skriv venligst dit fulde navn (f.eks. 'Anders Nielsen').",
            ConversationState.BOOKING_INTAKE_NAME,
        )

    upsert_student_name(db, conversation.phone_number, name)
    booking.pop("name_attempts", None)

    # If the reply combined a name with booking content, re-extract preferences
    # from the continuation and merge into context so the time/date/type isn't
    # lost when we resume the booking flow.
    if continuation:
        cont_prefs = extract_preferences(continuation)
        if cont_prefs.appointment_type and not booking.get("appointment_type"):
            booking["appointment_type"] = cont_prefs.appointment_type
        if cont_prefs.time_of_day and not booking.get("time_of_day"):
            booking["time_of_day"] = cont_prefs.time_of_day
        if cont_prefs.preferred_time and not booking.get("preferred_time"):
            booking["preferred_time"] = cont_prefs.preferred_time
        if cont_prefs.target_date and not booking.get("target_date"):
            booking["target_date"] = cont_prefs.target_date
        if cont_prefs.earliest_on_day and not booking.get("earliest_on_day"):
            booking["earliest_on_day"] = True
        if cont_prefs.latest_on_day and not booking.get("latest_on_day"):
            booking["latest_on_day"] = True
        if cont_prefs.date_range_start and cont_prefs.date_range_end and not booking.get("date_range_start"):
            booking["date_range_start"] = cont_prefs.date_range_start
            booking["date_range_end"] = cont_prefs.date_range_end

    # 999.37 pilot seed — first-booking onboarding now also captures the
    # student's lektionsplan position before offering slots. Any booking
    # continuation (type/time/date) is already stashed in context and resumes
    # after the lesson step via _resume_to_offer_or_type. The "Tak {name}!"
    # greeting moves onto the lesson prompt so the resume step doesn't repeat it.
    _save_context(conversation, context)
    if language == "en":
        return (
            f"Thanks {name}! Which lesson number are you on? "
            "Check your lesson plan and reply with the number (or 'don't know').",
            ConversationState.BOOKING_INTAKE_LESSON,
        )
    return (
        f"Tak {name}! Hvilken kørelektion er du på? "
        "Tjek din lektionsplan og skriv tallet (eller 'ved ikke').",
        ConversationState.BOOKING_INTAKE_LESSON,
    )


# Reasonable upper bound for a lektionsplan number (29 theory + 24 practical
# lektioner, numbered together → low-40s for the last road lessons; 60 leaves
# slack without accepting an obviously-bogus integer like a phone fragment).
_MAX_LESSON_NUMBER = 60
# Phrases that mean "I don't know my lesson number" — store NULL and move on.
_LESSON_UNKNOWN_RE = re.compile(
    r"\b(ved ikke|ved det ikke|aner det ikke|ingen anelse|ikke sikker|"
    r"don'?t know|dont know|no idea|not sure|unsure)\b",
    re.IGNORECASE,
)
_LESSON_NUMBER_RE = re.compile(r"\d+")


# Darkness-driving keywords. A booking request mentioning any of these is deflected
# to manual arrangement with the instructor (mørkekørsel = lektion 44, owner books
# it by hand). Catches explicit requests regardless of the stored lesson position.
_DARKNESS_RE = re.compile(
    r"\b(mørkekørsel|moerkekoersel|natkørsel|natkoersel|lygtekørsel|lygtekoersel|"
    r"lygtetænding|lygtetaending|kørsel i mørke|koersel i moerke|"
    r"darkness driving|night driving)\b",
    re.IGNORECASE,
)


def _darkness_manual_reply(
    db: Session,
    conversation: Conversation,
    language: str,
    lesson: int | None = None,
) -> tuple[str, ConversationState]:
    """Deflect a darkness / lektion-44 booking to manual arrangement with the owner.

    Lisa cannot auto-book mørkekørsel (evening/sunset-dependent); the owner schedules
    it by hand. Tells the student to arrange it with their instructor, notifies the
    owner (CC) so he can reach out, clears flow state, and ends in IDLE.
    """
    name = get_student_name(db, conversation.phone_number)
    who = f"{name} ({conversation.phone_number})" if name else conversation.phone_number
    tag = f" (lektion {lesson})" if lesson else ""
    cc_school_owner(
        f"{who} vil booke mørkekørsel{tag} — kan ikke bookes automatisk; "
        f"kontakt eleven for at aftale en aften."
    )
    _save_context(conversation, {})
    if language == "en":
        what = f"Lesson {lesson} (darkness driving)" if lesson else "Darkness driving"
        return (
            f"{what} can't be booked automatically yet — you arrange it directly with "
            f"your instructor. I've let the school know so they can find an evening with you.",
            ConversationState.IDLE,
        )
    what = f"Lektion {lesson} (mørkekørsel)" if lesson else "Mørkekørsel"
    return (
        f"{what} kan jeg ikke booke automatisk endnu — den aftales direkte med din "
        f"kørelærer. Jeg har givet skolen besked, så I kan finde en aften sammen.",
        ConversationState.IDLE,
    )


def _parse_lesson_number(body: str) -> int | None:
    """Extract a plausible lektionsplan number from a free-text reply, or None.

    Returns None for "ved ikke"-style replies and for anything with no integer
    or an out-of-range integer — the caller treats None as "unknown, proceed".
    """
    if _LESSON_UNKNOWN_RE.search(body):
        return None
    m = _LESSON_NUMBER_RE.search(body)
    if not m:
        return None
    n = int(m.group())
    if 1 <= n <= _MAX_LESSON_NUMBER:
        return n
    return None


def _resume_to_offer_or_type(
    db: Session,
    conversation: Conversation,
    language: str,
    context: dict,
) -> tuple[str, ConversationState]:
    """Resume the booking flow after first-booking onboarding (name + lesson).

    If a type was already stated/defaulted, jump straight to the earliest-slot
    offer; otherwise ask what to book. The name was thanked at the lesson prompt,
    so the type question here is plain (no repeated greeting).
    """
    booking = context.setdefault("booking", {})
    if booking.get("appointment_type"):
        _save_context(conversation, context)
        return _offer_earliest_slot(db, conversation, language, context)

    _save_context(conversation, context)
    if language == "en":
        return (
            "What would you like to book? Driving lesson, manoeuvring course, or driving test?",
            ConversationState.BOOKING_SELECT_TYPE,
        )
    return (
        "Hvad vil du booke? Køretime, manøvrebane eller prøve?",
        ConversationState.BOOKING_SELECT_TYPE,
    )


def _handle_intake_lesson(
    db: Session,
    conversation: Conversation,
    body: str,
    language: str,
    context: dict,
) -> tuple[str, ConversationState]:
    """999.37 pilot seed — capture the student's lektionsplan position.

    Tolerant by design: "ved ikke" / unparseable / out-of-range → store NULL and
    proceed (never block the booking). When a number maps to a known road-lesson
    content label, ask the student to verify it (the nudge) before continuing;
    otherwise just store it and move on.
    """
    lesson = _parse_lesson_number(body)
    set_student_lesson(db, conversation.phone_number, lesson)

    if lesson is not None:
        # Manual-only lesson (mørkekørsel / lektion 44): don't offer slots — deflect
        # to the instructor and notify the owner.
        if lesson in MANUAL_BOOKING_LESSONS:
            return _darkness_manual_reply(db, conversation, language, lesson=lesson)
        label = LEKTIONSPLAN_LABELS.get(lesson)
        if label:
            _save_context(conversation, context)
            if language == "en":
                return (
                    f"Lesson {lesson} is {label} — is that right? "
                    "Reply yes, or send the correct number.",
                    ConversationState.BOOKING_CONFIRM_LESSON,
                )
            return (
                f"Lektion {lesson} er {label} — passer det? "
                "Svar ja, eller skriv det rigtige nummer.",
                ConversationState.BOOKING_CONFIRM_LESSON,
            )

    # No number, unknown, or a number with no road-lesson label → nothing to
    # verify; continue straight to the offer (value already stored above).
    return _resume_to_offer_or_type(db, conversation, language, context)


def _handle_confirm_lesson(
    db: Session,
    conversation: Conversation,
    body: str,
    language: str,
    context: dict,
) -> tuple[str, ConversationState]:
    """999.37 pilot seed — handle the verify-your-lektion nudge reply.

    Any number in this reply is treated as a correction and stored (no re-nudge
    loop — verify once, then proceed). A reply with no number (an affirmation or
    anything else) keeps the stored value. Either way the booking flow continues
    — this step never blocks.
    """
    corrected = _parse_lesson_number(body)
    if corrected is not None:
        set_student_lesson(db, conversation.phone_number, corrected)
        if corrected in MANUAL_BOOKING_LESSONS:
            return _darkness_manual_reply(db, conversation, language, lesson=corrected)
    return _resume_to_offer_or_type(db, conversation, language, context)


def _handle_select_type(
    db: Session,
    conversation: Conversation,
    body: str,
    language: str,
    context: dict,
) -> tuple[str, ConversationState]:
    """Extract appointment type from reply. If recognized, offer earliest slot."""
    prefs = extract_preferences(body)
    if prefs.appointment_type:
        context.setdefault("booking", {})["appointment_type"] = prefs.appointment_type
        _save_context(conversation, context)
        return _offer_earliest_slot(db, conversation, language, context)

    # Could not extract type — track attempts and escalate after 3 failures
    context.setdefault("booking", {})
    attempts = context["booking"].get("type_attempts", 0) + 1
    context["booking"]["type_attempts"] = attempts
    if attempts >= 3:
        _save_context(conversation, {})
        return _fallback(language), ConversationState.IDLE

    _save_context(conversation, context)
    if language == "en":
        return (
            "Sorry, I didn't catch that. Please write: lesson, manoeuvring, or test.",
            ConversationState.BOOKING_SELECT_TYPE,
        )
    return (
        "Skriv venligst: køretime, manøvrebane eller prøve.",
        ConversationState.BOOKING_SELECT_TYPE,
    )


def _offer_slot_on_date(
    db: Session,
    conversation: Conversation,
    language: str,
    context: dict,
    appt_type: str,
    target_date,
    time_of_day: str | None = None,
    preferred_time: str | None = None,
    earliest_on_day: bool = False,
    latest_on_day: bool = False,
) -> tuple[str, ConversationState]:
    """Offer a slot on a single specific date.

    Used when the student has identified one concrete day. If no slots match
    on that day the reply is explicit ("no slots that day; nearest is X"),
    never silently shifted to another week. Clears the per-attempt
    target_date so a follow-up "ja" doesn't keep re-resolving the date.
    """
    # One-per-day cap, at the offer chokepoint: this is the single function that
    # offers for a concrete date, so guarding here covers BOTH fresh-entry and
    # mid-flow steering ("nej" then "i morgen kl 13") — the up-front check in
    # start_booking_flow only fires at entry. Deny instead of offering slots we'd
    # refuse at confirm. No-op when MAX_BOOKINGS_PER_DAY == 0.
    if MAX_BOOKINGS_PER_DAY > 0 and _booking_count_on_date(
        db, conversation.phone_number, target_date
    ) >= MAX_BOOKINGS_PER_DAY:
        _save_context(conversation, {})
        if language == "en":
            return (
                "You already have a lesson booked that day — only one per day for now. "
                "Pick another day and I'll find a time.",
                ConversationState.IDLE,
            )
        return (
            "Du har allerede en køretime den dag — der kan kun bookes én pr. dag indtil "
            "videre. Vælg en anden dag, så finder jeg en tid.",
            ConversationState.IDLE,
        )

    slots = find_slots_for_date(
        appt_type, target_date,
        time_of_day=time_of_day, preferred_time=preferred_time,
        earliest_on_day=earliest_on_day, latest_on_day=latest_on_day,
        min_spacing_minutes=60,
    )

    # type_defaulted (Phase 999.8 follow-up): when the appointment type was
    # silently defaulted from history rather than picked by the student, we
    # name the type in this first offer so they have a chance to override
    # before booking. Pop so follow-up offers don't keep repeating it.
    type_defaulted = bool(context["booking"].pop("type_defaulted", False))

    if slots:
        # earliest_on_day / latest_on_day already collapse to a single slot —
        # don't emit a "1) ..." prefix when the student explicitly asked for
        # the first/last; surface it as the legacy single-line offer.
        offered = slots[: 1 if (earliest_on_day or latest_on_day) else MAX_OFFER_SLOTS]
        _set_offered_slots(context, offered)
        _save_context(conversation, context)
        # Acknowledge when the student named a clock time we can't honor (±15 min)
        # instead of silently offering unrelated times.
        if (
            preferred_time
            and not earliest_on_day
            and not latest_on_day
            and not preferred_time_satisfied(offered, preferred_time)
        ):
            return _emit_time_unavailable_offer(language, preferred_time, offered)
        if len(offered) == 1:
            formatted = _format_slot(offered[0])
            return _emit_single_offer(language, formatted, appt_type, type_defaulted)
        body = _format_multi_offer(offered, language)
        return _emit_multi_offer(language, body, appt_type, type_defaulted)

    # No matching slots on the requested day — surface the closest fallback
    # explicitly so the student understands the day is unavailable.
    alternative = find_nearest_alternative(appt_type)
    # The target_date is consumed; drop it so the student's next reply is
    # interpreted in the multi-day flow.
    context["booking"].pop("target_date", None)
    if alternative is None:
        _save_context(conversation, {})
        if language == "en":
            return (
                "Sorry, no available slots in the next 2 weeks. We'll get back to you.",
                ConversationState.IDLE,
            )
        return (
            "Desværre ingen ledige tider de næste 2 uger. Vi vender tilbage.",
            ConversationState.IDLE,
        )

    _set_offered_slots(context, [alternative])
    _save_context(conversation, context)
    formatted = _format_slot(alternative)
    day_label = target_date.strftime("%d/%-m")
    if language == "en":
        return (
            f"No slots on {day_label}. Closest available: {formatted}. Does that work?",
            ConversationState.BOOKING_OFFER_SLOT,
        )
    return (
        f"Ingen ledige tider {day_label}. Nærmeste: {formatted}. Passer det?",
        ConversationState.BOOKING_OFFER_SLOT,
    )


def _offer_slot_in_range(
    db: Session,
    conversation: Conversation,
    language: str,
    context: dict,
    appt_type: str,
    range_start,
    range_end,
    time_of_day: str | None = None,
    preferred_time: str | None = None,
) -> tuple[str, ConversationState]:
    """Offer slot(s) within an inclusive [range_start, range_end] window.

    Used when the student asked for "i naeste uge" / "foer den 20." / etc.
    and the LLM extractor returned date_range_start + date_range_end. Bounded
    search — never fall through past range_end. Collapses to the single-day
    path when start == end so the existing target_date copy applies.

    Cleared from context after use so a follow-up reply doesn't keep applying
    the stale range.
    """
    if range_start == range_end:
        # Range collapses to a single day — delegate to the single-day path.
        return _offer_slot_on_date(
            db, conversation, language, context, appt_type, range_start,
            time_of_day=time_of_day, preferred_time=preferred_time,
        )

    slots = find_slots_in_range(
        appt_type, range_start, range_end,
        time_of_day=time_of_day, preferred_time=preferred_time,
        max_slots=MAX_OFFER_SLOTS,
        min_spacing_minutes=60,
    )

    type_defaulted = bool(context["booking"].pop("type_defaulted", False))

    if slots:
        offered = slots[:MAX_OFFER_SLOTS]
        _set_offered_slots(context, offered)
        # Range consumed — drop so a "ja"/"nej" follow-up doesn't re-search.
        context["booking"].pop("date_range_start", None)
        context["booking"].pop("date_range_end", None)
        _save_context(conversation, context)
        if len(offered) == 1:
            formatted = _format_slot(offered[0])
            if type_defaulted:
                return _emit_single_offer(language, formatted, appt_type, type_defaulted=True)
            # Range single-slot keeps its original "Hvad med X" phrasing
            # (different from the cold-start "Næste ledige tid er X" copy).
            if language == "en":
                return (
                    f"How about {formatted}? Reply 'yes' to confirm or write another day or time.",
                    ConversationState.BOOKING_OFFER_SLOT,
                )
            return (
                f"Hvad med {formatted}? Svar 'ja' for at bekræfte eller skriv en anden dag eller et tidspunkt.",
                ConversationState.BOOKING_OFFER_SLOT,
            )
        body = _format_multi_offer(offered, language)
        return _emit_multi_offer(language, body, appt_type, type_defaulted)

    # No slots in the requested range — surface the closest alternative
    # explicitly so the student understands the window is unavailable.
    alternative = find_nearest_alternative(appt_type)
    context["booking"].pop("date_range_start", None)
    context["booking"].pop("date_range_end", None)
    if alternative is None:
        _save_context(conversation, {})
        if language == "en":
            return (
                "Sorry, no available slots in the next 2 weeks. We'll get back to you.",
                ConversationState.IDLE,
            )
        return (
            "Desværre ingen ledige tider de næste 2 uger. Vi vender tilbage.",
            ConversationState.IDLE,
        )

    _set_offered_slots(context, [alternative])
    _save_context(conversation, context)
    formatted = _format_slot(alternative)
    range_label = f"{range_start.strftime('%d/%-m')}-{range_end.strftime('%d/%-m')}"
    if language == "en":
        return (
            f"No slots in {range_label}. Closest available: {formatted}. Does that work?",
            ConversationState.BOOKING_OFFER_SLOT,
        )
    return (
        f"Ingen ledige tider {range_label}. Nærmeste: {formatted}. Passer det?",
        ConversationState.BOOKING_OFFER_SLOT,
    )


def _offer_earliest_slot(
    db: Session,
    conversation: Conversation,
    language: str,
    context: dict,
) -> tuple[str, ConversationState]:
    """Find earliest slot for the appointment type, format it, and ask for confirmation (per D-01)."""
    appt_type = context["booking"]["appointment_type"]
    time_of_day = context["booking"].get("time_of_day")
    preferred_time = context["booking"].get("preferred_time")
    target_date_str = context["booking"].get("target_date")
    earliest_on_day = bool(context["booking"].get("earliest_on_day", False))
    latest_on_day = bool(context["booking"].get("latest_on_day", False))
    range_start_str = context["booking"].get("date_range_start")
    range_end_str = context["booking"].get("date_range_end")

    # Phase 999.34: the band menu ("Morgen/formiddag/eftermiddag/aften?") is
    # GONE. A fluffy request (no time given) now leads straight with the
    # earliest slots; the student steers afterwards with a day and/or a time as
    # free text (still understood — the extractor parses time_of_day /
    # preferred_time — we just never present the band as a question). Falls
    # through to the earliest-slot search below.

    # When the LLM resolved a specific calendar date ("denne sondag",
    # "i morgen") route through the single-day search so we never
    # silently jump to a different week. Falls through to the generic
    # earliest-slot search if parsing fails.
    if target_date_str:
        try:
            from datetime import date as _date_cls
            tgt = _date_cls.fromisoformat(target_date_str)
        except ValueError:
            tgt = None
        if tgt is not None:
            return _offer_slot_on_date(
                db, conversation, language, context, appt_type, tgt,
                time_of_day=time_of_day, preferred_time=preferred_time,
                earliest_on_day=earliest_on_day, latest_on_day=latest_on_day,
            )

    # Range takes precedence over the multi-day weekday-list fallback —
    # "i naeste uge" must never silently shift to week +2.
    if range_start_str and range_end_str:
        try:
            from datetime import date as _date_cls
            rs = _date_cls.fromisoformat(range_start_str)
            re_ = _date_cls.fromisoformat(range_end_str)
        except ValueError:
            rs = re_ = None
        if rs is not None and re_ is not None and rs <= re_:
            return _offer_slot_in_range(
                db, conversation, language, context, appt_type, rs, re_,
                time_of_day=time_of_day, preferred_time=preferred_time,
            )

    slots = find_earliest_slots(
        appt_type, n=MAX_OFFER_SLOTS,
        time_of_day=time_of_day, preferred_time=preferred_time,
        min_spacing_minutes=60,
    )
    type_defaulted = bool(context["booking"].pop("type_defaulted", False))
    if not slots and time_of_day:
        # No slots in preferred time — fall back to a single nearest overall
        # (intentionally not multi: the student's preference already failed,
        # so don't bury the fallback in a numbered list).
        slot = find_earliest_slot(appt_type)
        if slot:
            _set_offered_slots(context, [slot])
            _save_context(conversation, context)
            formatted = _format_slot(slot)
            if language == "en":
                return (
                    f"No slots in that time window. Nearest available: {formatted}. Does that work?",
                    ConversationState.BOOKING_OFFER_SLOT,
                )
            return (
                f"Ingen ledige tider i det tidsrum. Nærmeste: {formatted}. Passer det?",
                ConversationState.BOOKING_OFFER_SLOT,
            )
    if not slots:
        _save_context(conversation, {})
        if language == "en":
            return (
                "Sorry, no available slots in the next 2 weeks. We'll get back to you.",
                ConversationState.IDLE,
            )
        return (
            "Desværre ingen ledige tider de næste 2 uger. Vi vender tilbage.",
            ConversationState.IDLE,
        )

    _set_offered_slots(context, slots)
    _save_context(conversation, context)
    # Acknowledge when the student named a clock time we can't honor (±15 min).
    if preferred_time and not preferred_time_satisfied(slots, preferred_time):
        return _emit_time_unavailable_offer(language, preferred_time, slots)
    if len(slots) == 1:
        formatted = _format_slot(slots[0])
        return _emit_single_offer(language, formatted, appt_type, type_defaulted)
    body = _format_multi_offer(slots, language)
    return _emit_multi_offer(language, body, appt_type, type_defaulted)


def _format_pref_time(preferred_time: str) -> str:
    """Render a HH:MM preferred time compactly: '12:00' -> 'kl. 12', '12:30' -> 'kl. 12:30'."""
    try:
        h, m = map(int, preferred_time.split(":"))
    except (ValueError, AttributeError):
        return preferred_time
    return f"kl. {h}" if m == 0 else f"kl. {h}:{m:02d}"


def _emit_time_unavailable_offer(
    language: str,
    preferred_time: str,
    slots: list[datetime],
) -> tuple[str, ConversationState]:
    """Offer when the student's requested clock time isn't available (±15 min).

    Acknowledges the requested time explicitly — "Kl. 12 er ikke ledig, men …" —
    instead of silently offering unrelated times (which read as Lisa ignoring
    the request). Falls back to single- or multi-slot phrasing for the list.
    """
    pref = _format_pref_time(preferred_time)
    if len(slots) == 1:
        formatted = _format_slot(slots[0])
        if language == "en":
            return (
                f"{preferred_time} isn't available. Closest: {formatted}. Does that work?",
                ConversationState.BOOKING_OFFER_SLOT,
            )
        return (
            f"{pref.capitalize()} er ikke ledig. Nærmeste: {formatted}. Passer det?",
            ConversationState.BOOKING_OFFER_SLOT,
        )
    body = _format_multi_offer(slots, language)
    if language == "en":
        return (
            f"{preferred_time} isn't available. Available times: {body}",
            ConversationState.BOOKING_OFFER_SLOT,
        )
    return (
        f"{pref.capitalize()} er ikke ledig, men ledige tider er: {body}",
        ConversationState.BOOKING_OFFER_SLOT,
    )


def _emit_single_offer(
    language: str,
    formatted_slot: str,
    appt_type: str,
    type_defaulted: bool,
) -> tuple[str, ConversationState]:
    """Format the single-slot offer SMS.

    When type_defaulted=True, the type is named in the lead-in
    ("Næste ledige køretime er ...") so a returning student whose type was
    silently defaulted from history gets a chance to override before
    confirming. Otherwise the type-agnostic copy is used.
    """
    if type_defaulted:
        if language == "en":
            label = APPT_TYPE_NAMES_EN.get(appt_type, "slot")
            return (
                f"Next available {label}: {formatted_slot}. Does that work? "
                f"Otherwise tell me when — a day (\"Tuesday\") or a time (\"afternoon\" or \"2pm\").",
                ConversationState.BOOKING_OFFER_SLOT,
            )
        label = APPT_TYPE_NAMES_DA.get(appt_type, "tid")
        return (
            f"Næste ledige {label} er {formatted_slot}. Passer det? "
            f"Ellers skriv hvornår du helst vil — fx en dag (\"tirsdag\") eller et tidspunkt (\"eftermiddag\" eller \"kl 14\").",
            ConversationState.BOOKING_OFFER_SLOT,
        )
    if language == "en":
        return (
            f"Next available: {formatted_slot}. Does that work? "
            f"Otherwise tell me when — a day (\"Tuesday\") or a time (\"afternoon\" or \"2pm\").",
            ConversationState.BOOKING_OFFER_SLOT,
        )
    return (
        f"Næste ledige tid er {formatted_slot}. Passer det? "
        f"Ellers skriv hvornår du helst vil — fx en dag (\"tirsdag\") eller et tidspunkt (\"eftermiddag\" eller \"kl 14\").",
        ConversationState.BOOKING_OFFER_SLOT,
    )


def _emit_multi_offer(
    language: str,
    body: str,
    appt_type: str,
    type_defaulted: bool,
) -> tuple[str, ConversationState]:
    """Format the multi-slot offer SMS. Mirrors _emit_single_offer's
    type-mention behaviour: when type_defaulted=True, names the (plural)
    type in the lead-in ("Ledige køretimer: …")."""
    # Phase 999.34: a compact steer hint follows the numbered list so the
    # student knows they can narrow by a different day or time instead of picking.
    steer_da = " Ellers skriv en dag eller et tidspunkt."
    steer_en = " Or write a day or a time."
    if type_defaulted:
        if language == "en":
            label = APPT_TYPE_PLURAL_EN.get(appt_type, "slots")
            return (
                f"Available {label}: {body}.{steer_en}",
                ConversationState.BOOKING_OFFER_SLOT,
            )
        label = APPT_TYPE_PLURAL_DA.get(appt_type, "tider")
        return (
            f"Ledige {label}: {body}.{steer_da}",
            ConversationState.BOOKING_OFFER_SLOT,
        )
    if language == "en":
        return (
            f"Available slots: {body}.{steer_en}",
            ConversationState.BOOKING_OFFER_SLOT,
        )
    return (
        f"Ledige tider: {body}.{steer_da}",
        ConversationState.BOOKING_OFFER_SLOT,
    )


_CONFIRM_WORDS = {"ja", "yes", "ok", "jep", "jo", "y", "yep", "sure"}

# 0-indexed pick mapped from each accepted token. Order matters: longer/more
# specific tokens first so substring matches don't fire on the wrong index.
#
# Whole-body match is lenient — when the entire normalized body equals a
# token, the intent is unambiguous regardless of the word's other meanings
# ("en" alone means slot 1; "anden" alone means slot 2).
_PICK_TOKENS: dict[str, int] = {
    # numerals
    "1": 0, "2": 1, "3": 2,
    "1)": 0, "2)": 1, "3)": 2,
    # Danish cardinal / ordinal
    "én": 0, "en": 0, "to": 1, "tre": 2,
    "første": 0, "anden": 1, "tredje": 2,
    "nr.1": 0, "nr.2": 1, "nr.3": 2,
    "nr1": 0, "nr2": 1, "nr3": 2,
    # English
    "one": 0, "two": 1, "three": 2,
    "first": 0, "second": 1, "third": 2,
}

# Multi-word match is strict — only tokens with no plausible non-pick
# meaning. Word-forms like "en" (DA indefinite article), "anden" (DA "another"),
# "to" (EN preposition), "one" (EN pronoun), "first/second" (EN time/sequence
# adjectives) are EXCLUDED because they appear in normal questions
# ("Hvordan booker jeg EN køretime?", "Kan jeg få en ANDEN tid?").
# Students who want to pick mid-sentence can write the digit or "én".
_PICK_TOKENS_STRICT: dict[str, int] = {
    "1": 0, "2": 1, "3": 2,
    "1)": 0, "2)": 1, "3)": 2,
    "én": 0,
    "nr.1": 0, "nr.2": 1, "nr.3": 2,
    "nr1": 0, "nr2": 1, "nr3": 2,
}


def _parse_pick(body: str, num_offered: int) -> int | None:
    """Parse a slot pick from the student's reply.

    Returns 0-indexed slot position if the body unambiguously identifies a
    pick AND that index is within the offered list. Returns None when no
    pick is recognised (caller falls through to the existing "ja" /
    preference-extraction branches).
    """
    if num_offered <= 1:
        return None
    body_norm = body.strip().lower()
    if not body_norm:
        return None
    # Whole-body match first ("2", "to", "nr 2", "anden")
    compact = body_norm.replace(" ", "")
    if compact in _PICK_TOKENS:
        idx = _PICK_TOKENS[compact]
        return idx if idx < num_offered else None
    # Fall back to first matching strict-pick token in a multi-word reply
    # ("jeg vælger 2", "ja, 2", "tag nr 3"). Uses the strict dict that
    # excludes ambiguous word-forms — see comment on _PICK_TOKENS_STRICT.
    # Skip confirm words so "ja, 2" still picks 2.
    tokens = body_norm.replace(",", " ").replace(".", " ").split()
    for tok in tokens:
        if tok in _CONFIRM_WORDS:
            continue
        if tok in _PICK_TOKENS_STRICT:
            idx = _PICK_TOKENS_STRICT[tok]
            return idx if idx < num_offered else None
    return None


def _handle_offer_reply(
    db: Session,
    conversation: Conversation,
    body: str,
    language: str,
    context: dict,
) -> tuple[str, ConversationState]:
    """Student replied to slot offer. Check if they confirmed or want different days."""
    booking = context.get("booking", {})
    offered_slots = booking.get("offered_slots") or []
    offered_slot_iso = booking.get("offered_slot")  # ISO string set by _set_offered_slots

    # Phase 999.7: mid-flow FAQ pass-through.
    # Cap at MAX_MID_FLOW_FAQ_PASSTHROUGHS (=2) per offer attempt. The third
    # unrelated/FAQ-looking message falls through to the existing
    # offer_attempts re-prompt → escalate path (current behaviour preserved).
    #
    # The FAQ branch goes BEFORE the _CONFIRM_WORDS / _parse_pick / preferences-
    # extraction branches: try_mid_flow_faq returns None for non-FAQ intents so
    # confirm words and slot picks still fall through to the existing logic
    # unchanged. Only FAQ-class intents at confidence >= 0.7 are answered here.
    current_count = booking.get("mid_flow_faq_count", 0)
    if current_count < MAX_MID_FLOW_FAQ_PASSTHROUGHS:
        faq_reply = try_mid_flow_faq(
            body=body,
            language=language,
            # Mid-flow conversation context is not threaded through
            # handle_booking_step today; the classifier gets the body alone.
            # If accuracy degrades in production, plumb
            # manager._get_conversation_context into handle_booking_step and
            # pass it here. Tracked separately.
            conversation_context="",
            offered_slot_iso=offered_slot_iso,
        )
        if faq_reply is not None:
            increment_faq_count(context, "booking")
            _save_context(conversation, context)
            return faq_reply, ConversationState.BOOKING_OFFER_SLOT

    # Multi-slot pick: "1" / "2" / "3" / "anden" etc. — only relevant when
    # 2+ slots were offered. Resolves to a 0-indexed position into
    # offered_slots; we then mirror the chosen ISO into offered_slot so
    # _confirm_booking proceeds against it unchanged.
    if len(offered_slots) > 1:
        pick = _parse_pick(body, len(offered_slots))
        if pick is not None:
            chosen_iso = offered_slots[pick]
            booking["offered_slot"] = chosen_iso
            booking.pop("offered_slots", None)
            _save_context(conversation, context)
            # Echo the chosen slot and ask for a final confirmation instead of
            # booking immediately — prevents accidental bookings from a mistyped
            # number. offered_slots is now empty, so the student's "ja" falls
            # through to the _CONFIRM_WORDS branch → _confirm_booking unchanged.
            formatted = _format_slot(datetime.fromisoformat(chosen_iso))
            if language == "en":
                return (
                    f"You chose {formatted}. Shall we book it? Reply YES or NO.",
                    ConversationState.BOOKING_OFFER_SLOT,
                )
            return (
                f"Du har valgt {formatted}. Skal vi booke den? Svar JA eller NEJ.",
                ConversationState.BOOKING_OFFER_SLOT,
            )

    # Check for confirmation (Phase 999.1: natural-language, not just single words).
    # _parse_pick already ran above, so "ja, 2" went to slot-pick; is_affirmation
    # rejects anything with a number/weekday/time so only a plain yes lands here.
    if is_affirmation(body):
        return _confirm_booking(db, conversation, language, context)

    # Not a confirmation — extract preferences. Three branches:
    #  (a) time-only adjustment (e.g. "kl 09 passer bedre") — keep previously
    #      stored preferred_days and re-search with the new time. Without
    #      this branch, time-only replies fell through to "Svar 'ja' for at
    #      bekræfte" and the student's preference was silently ignored.
    #  (b) day(s) / range / target_date supplied (with or without time).
    #  (c) nothing parseable — increment offer_attempts and re-prompt.
    prefs = extract_preferences(body)
    has_time = bool(prefs.preferred_time or prefs.time_of_day)
    has_range = bool(prefs.date_range_start and prefs.date_range_end)
    if has_time and not prefs.days and not prefs.target_date and not has_range:
        bk = context.setdefault("booking", {})
        if prefs.preferred_time:
            bk["preferred_time"] = prefs.preferred_time
        if prefs.time_of_day:
            bk["time_of_day"] = prefs.time_of_day
        bk["offer_attempts"] = 0
        _save_context(conversation, context)
        return _search_and_offer(db, conversation, language, context)

    if prefs.days or prefs.target_date or has_range:
        bk = context.setdefault("booking", {})
        if prefs.days:
            bk["preferred_days"] = prefs.days
        if prefs.target_date:
            bk["target_date"] = prefs.target_date
            # earliest/latest only apply to a target_date — refresh both flags
            # from the new prefs so a follow-up "tidligste tid" works mid-flow.
            if prefs.earliest_on_day:
                bk["earliest_on_day"] = True
            else:
                bk.pop("earliest_on_day", None)
            if prefs.latest_on_day:
                bk["latest_on_day"] = True
            else:
                bk.pop("latest_on_day", None)
            # New target_date supersedes any stale range from a prior turn.
            bk.pop("date_range_start", None)
            bk.pop("date_range_end", None)
        elif has_range:
            # Student switched to a range — drop stale single-day target +
            # earliest/latest, install new range.
            bk["date_range_start"] = prefs.date_range_start
            bk["date_range_end"] = prefs.date_range_end
            bk.pop("target_date", None)
            bk.pop("earliest_on_day", None)
            bk.pop("latest_on_day", None)
        else:
            # Student switched to a multi-day weekday request — drop any
            # stale single-day target / range.
            bk.pop("target_date", None)
            bk.pop("earliest_on_day", None)
            bk.pop("latest_on_day", None)
            bk.pop("date_range_start", None)
            bk.pop("date_range_end", None)
        if prefs.time_of_day:
            bk["time_of_day"] = prefs.time_of_day
        if prefs.preferred_time:
            bk["preferred_time"] = prefs.preferred_time
        # Phase 999.34: no band menu. Search with whatever the student gave —
        # days and/or time/date/range. A days-only steer offers the earliest
        # slots on those days; the student can narrow by time on the next turn.
        _save_context(conversation, context)
        return _search_and_offer(db, conversation, language, context)

    # Could not parse — track attempts and escalate after 3 failures
    context.setdefault("booking", {})
    attempts = context["booking"].get("offer_attempts", 0) + 1
    context["booking"]["offer_attempts"] = attempts
    if attempts >= 3:
        # A student actively trying to book whom Lisa can't parse after 3 tries
        # is a warm lead — hand it to the owner instead of dropping it with a
        # generic "something went wrong".
        _save_context(conversation, {})
        return (
            forward_to_owner(db, conversation, body, reason="booking_offer_3x", language=language),
            ConversationState.IDLE,
        )

    _save_context(conversation, context)
    if language == "en":
        return (
            "Reply 'yes' to confirm, or write a day or a time that suits you.",
            ConversationState.BOOKING_OFFER_SLOT,
        )
    return (
        "Svar 'ja' for at bekræfte, eller skriv en dag eller et tidspunkt der passer dig.",
        ConversationState.BOOKING_OFFER_SLOT,
    )


def _handle_select_day(
    db: Session,
    conversation: Conversation,
    body: str,
    language: str,
    context: dict,
) -> tuple[str, ConversationState]:
    """Student sends preferred days (per D-02 flow)."""
    prefs = extract_preferences(body)
    if prefs.days:
        bk = context.setdefault("booking", {})
        bk["preferred_days"] = prefs.days
        bk.pop("day_attempts", None)
        # Phase 999.34: no band menu — search the chosen days for the earliest
        # slots. Carry any time the student volunteered alongside the day.
        if prefs.time_of_day:
            bk["time_of_day"] = prefs.time_of_day
        if prefs.preferred_time:
            bk["preferred_time"] = prefs.preferred_time
        _save_context(conversation, context)
        return _search_and_offer(db, conversation, language, context)

    # Could not parse days — track attempts and escalate after 3 (#3 fix:
    # this step previously re-prompted forever with no cap, unlike name/time/
    # offer which all forward to the owner after 3 failures).
    bk = context.setdefault("booking", {})
    attempts = bk.get("day_attempts", 0) + 1
    bk["day_attempts"] = attempts
    if attempts >= 3:
        _save_context(conversation, {})
        return (
            forward_to_owner(db, conversation, body, reason="booking_day_3x", language=language),
            ConversationState.IDLE,
        )
    _save_context(conversation, context)
    if language == "en":
        return (
            "Which days work for you? E.g. 'Tuesday and Wednesday'.",
            ConversationState.BOOKING_SELECT_DAY,
        )
    return (
        "Hvilke dage passer dig? F.eks. 'tirsdag og onsdag'.",
        ConversationState.BOOKING_SELECT_DAY,
    )


def _handle_select_time(
    db: Session,
    conversation: Conversation,
    body: str,
    language: str,
    context: dict,
) -> tuple[str, ConversationState]:
    """Student sends time of day (per D-02 flow), then search and offer.

    The band reply often carries a day/date too ("tirsdag eftermiddag",
    "i morgen formiddag"). Capture any day / target_date / range / specific
    time alongside the band — otherwise a combined answer was reduced to
    band-only, dropping the requested day and offering the soonest slot on ANY
    day (e.g. "tirsdag eftermiddag" -> Saturday afternoon).
    """
    prefs = extract_preferences(body)
    bk = context.setdefault("booking", {})

    # Capture day/date/range info first so it survives even if no band is given
    # yet (a follow-up band reply then uses it). target_date wins over range
    # wins over a plain days list — mirrors _handle_offer_reply.
    if prefs.target_date:
        bk["target_date"] = prefs.target_date
        if prefs.earliest_on_day:
            bk["earliest_on_day"] = True
        else:
            bk.pop("earliest_on_day", None)
        if prefs.latest_on_day:
            bk["latest_on_day"] = True
        else:
            bk.pop("latest_on_day", None)
        bk.pop("date_range_start", None)
        bk.pop("date_range_end", None)
    elif prefs.date_range_start and prefs.date_range_end:
        bk["date_range_start"] = prefs.date_range_start
        bk["date_range_end"] = prefs.date_range_end
        bk.pop("target_date", None)
    elif prefs.days:
        bk["preferred_days"] = prefs.days

    if prefs.preferred_time:
        bk["preferred_time"] = prefs.preferred_time

    if prefs.time_of_day or prefs.preferred_time:
        if prefs.time_of_day:
            bk["time_of_day"] = prefs.time_of_day
        bk.pop("time_attempts", None)
        _save_context(conversation, context)
        return _search_and_offer(db, conversation, language, context)

    # No band yet — track attempts and escalate after 3 failures. Any day/date
    # captured above is persisted so a follow-up band reply still uses it.
    attempts = bk.get("time_attempts", 0) + 1
    bk["time_attempts"] = attempts
    if attempts >= 3:
        _save_context(conversation, {})
        return (
            forward_to_owner(db, conversation, body, reason="booking_time_3x", language=language),
            ConversationState.IDLE,
        )

    _save_context(conversation, context)
    if language == "en":
        return (
            "Please write: morning, before noon, afternoon, or evening.",
            ConversationState.BOOKING_SELECT_TIME,
        )
    return (
        "Skriv venligst: morgen, formiddag, eftermiddag eller aften.",
        ConversationState.BOOKING_SELECT_TIME,
    )


def _search_and_offer(
    db: Session,
    conversation: Conversation,
    language: str,
    context: dict,
) -> tuple[str, ConversationState]:
    """Find slots matching preferences (per D-04), offer best fit or nearest alternative (per D-07)."""
    booking = context.get("booking", {})
    appt_type = booking.get("appointment_type", "lesson")
    days = booking.get("preferred_days", [])
    time_of_day = booking.get("time_of_day")
    preferred_time = booking.get("preferred_time")
    target_date_str = booking.get("target_date")
    earliest_on_day = bool(booking.get("earliest_on_day", False))
    latest_on_day = bool(booking.get("latest_on_day", False))
    range_start_str = booking.get("date_range_start")
    range_end_str = booking.get("date_range_end")

    # Single-day request takes precedence — never silently fall through to a
    # different week when the student named a specific day.
    if target_date_str:
        try:
            from datetime import date as _date_cls
            tgt = _date_cls.fromisoformat(target_date_str)
        except ValueError:
            tgt = None
        if tgt is not None:
            return _offer_slot_on_date(
                db, conversation, language, context, appt_type, tgt,
                time_of_day=time_of_day, preferred_time=preferred_time,
                earliest_on_day=earliest_on_day, latest_on_day=latest_on_day,
            )

    # Range takes precedence over the weekday-list fallback (Phase 999.5).
    if range_start_str and range_end_str:
        try:
            from datetime import date as _date_cls
            rs = _date_cls.fromisoformat(range_start_str)
            re_ = _date_cls.fromisoformat(range_end_str)
        except ValueError:
            rs = re_ = None
        if rs is not None and re_ is not None and rs <= re_:
            return _offer_slot_in_range(
                db, conversation, language, context, appt_type, rs, re_,
                time_of_day=time_of_day, preferred_time=preferred_time,
            )

    slots = find_slots_for_preferences(
        appt_type, days, time_of_day, preferred_time=preferred_time,
        min_spacing_minutes=60,
    )
    if slots:
        offered = slots[:MAX_OFFER_SLOTS]
        _set_offered_slots(context, offered)
        _save_context(conversation, context)
        if len(offered) == 1:
            formatted = _format_slot(offered[0])
            if language == "en":
                return (
                    f"How about {formatted}? Reply 'yes' to confirm or write another day or time.",
                    ConversationState.BOOKING_OFFER_SLOT,
                )
            return (
                f"Hvad med {formatted}? Svar 'ja' for at bekræfte eller skriv en anden dag eller et tidspunkt.",
                ConversationState.BOOKING_OFFER_SLOT,
            )
        body = _format_multi_offer(offered, language)
        if language == "en":
            return (
                f"Available slots: {body}",
                ConversationState.BOOKING_OFFER_SLOT,
            )
        return (
            f"Ledige tider: {body}",
            ConversationState.BOOKING_OFFER_SLOT,
        )

    # Per D-07: No matching slots — suggest nearest alternative + ask for new preferences
    alternative = find_nearest_alternative(appt_type)
    if alternative:
        _set_offered_slots(context, [alternative])
        # Clear day/time prefs so student can start fresh
        context["booking"].pop("preferred_days", None)
        context["booking"].pop("time_of_day", None)
        _save_context(conversation, context)
        formatted = _format_slot(alternative)
        if language == "en":
            return (
                f"No slots for your preference. Nearest: {formatted}. OK? Or write another day or time.",
                ConversationState.BOOKING_OFFER_SLOT,
            )
        return (
            f"Ingen ledige tider der. Nærmeste: {formatted}. Passer det? Ellers skriv en anden dag eller et tidspunkt.",
            ConversationState.BOOKING_OFFER_SLOT,
        )

    _save_context(conversation, {})
    if language == "en":
        return "Sorry, no available slots right now. We'll get back to you.", ConversationState.IDLE
    return "Desværre ingen ledige tider lige nu. Vi vender tilbage.", ConversationState.IDLE


def _confirm_booking(
    db: Session,
    conversation: Conversation,
    language: str,
    context: dict,
) -> tuple[str, ConversationState]:
    """Create Booking record, write to Google Calendar, notify owner (per BOOK-03)."""
    booking_ctx = context.get("booking", {})
    slot_str = booking_ctx.get("offered_slot")
    appt_type_name = booking_ctx.get("appointment_type", "lesson")

    if not slot_str:
        _save_context(conversation, {})
        return _fallback(language), ConversationState.IDLE

    start_dt = datetime.fromisoformat(slot_str)
    appt_types = load_appointment_types()
    at = appt_types.get(appt_type_name)
    if not at:
        _save_context(conversation, {})
        return _fallback(language), ConversationState.IDLE

    end_dt = start_dt + timedelta(minutes=at.duration_minutes)

    # Finding 1 (booking audit) — past-slot guard. The offer-side filter
    # (get_slots_for_date) only guarantees future-ness when the slot was
    # OFFERED; a student who delays their "ja" can otherwise confirm a slot that
    # has since passed (same class as e428c5d, but at the confirm step).
    # _now_local is a patchable seam so the time-travelling test suite (mocked
    # historical slots) is unaffected.
    if start_dt <= _now_local():
        _save_context(conversation, {})
        if language == "en":
            return (
                "Sorry, that time has now passed. Send a new message to find a new time.",
                ConversationState.IDLE,
            )
        return (
            "Den tid er desværre passeret. Send en ny besked for at finde en ny tid.",
            ConversationState.IDLE,
        )

    # One-booking-per-day cap. Checked at confirm time against the chosen slot's
    # date so it also catches a student who books a second slot on a day they
    # already hold one (the total-outstanding cap alone allows two in one day).
    # No-op when MAX_BOOKINGS_PER_DAY == 0.
    if MAX_BOOKINGS_PER_DAY > 0 and _booking_count_on_date(
        db, conversation.phone_number, start_dt.date()
    ) >= MAX_BOOKINGS_PER_DAY:
        _save_context(conversation, {})
        if language == "en":
            return (
                "You already have a lesson that day — only one booking per day for now. "
                "Send a new message to pick another day.",
                ConversationState.IDLE,
            )
        return (
            "Du har allerede en køretime den dag — der kan kun bookes én pr. dag indtil "
            "videre. Send en ny besked for at vælge en anden dag.",
            ConversationState.IDLE,
        )

    # Finding 2 (booking audit) — re-check availability at confirmation time
    # against ALL busy sources, not just the Booking table. get_busy_intervals
    # unions confirmed Bookings AND cached owner-calendar events across every
    # calendar (999.30), so it also catches the owner adding a real commitment
    # over the offered slot between offer and confirm. Cache-bounded (up to the
    # 15-min poll); the partial unique index / IntegrityError on flush below is
    # the ultimate same-slot-race backstop. DB-only — no CalDAV on this path.
    #
    # Catches:
    #   - Two students racing on the same slot inside the cache window
    #   - The owner blocking the slot on any of his calendars mid-conversation
    #   - The same student being re-offered a slot they just booked
    busy = get_busy_intervals(start_dt.date(), session=db)
    conflict = any(b_start < end_dt and b_end > start_dt for b_start, b_end in busy)
    if conflict:
        _save_context(conversation, {})
        if language == "en":
            return (
                "Sorry, that slot was just taken. Send a new message to search again.",
                ConversationState.IDLE,
            )
        return (
            "Den tid er desværre netop taget. Send en ny besked for at søge igen.",
            ConversationState.IDLE,
        )

    # Insert the Booking row BEFORE writing to GCal. Two reasons:
    #   1. The partial unique index on (start_dt, end_dt) WHERE status='confirmed'
    #      catches the cross-student same-slot race here via IntegrityError on flush,
    #      before we've created an orphan GCal event.
    #   2. If GCal write fails after this point, we still want to persist the
    #      student's confirmation and alert the owner — preserving "student
    #      confirmed but kalender not updated" semantics.
    booking = Booking(
        phone_number=conversation.phone_number,
        conversation_id=conversation.id,
        appointment_type=appt_type_name,
        start_dt=start_dt,   # naive Copenhagen local
        end_dt=end_dt,       # naive Copenhagen local
        gcal_event_id=None,  # filled in after GCal write succeeds
        status="confirmed",
    )
    db.add(booking)
    try:
        db.flush()
    except IntegrityError:
        # Same-slot race — another student's confirmation reached the DB first.
        db.rollback()
        _save_context(conversation, {})
        if language == "en":
            return (
                "Sorry, that slot was just taken. Send a new message to search again.",
                ConversationState.IDLE,
            )
        return (
            "Den tid er desværre netop taget. Send en ny besked for at søge igen.",
            ConversationState.IDLE,
        )

    # Write to Google Calendar (timezone-aware for GCal API)
    # Slot engine produces naive Copenhagen local times, so tag them as Copenhagen
    start_aware = start_dt.replace(tzinfo=COPENHAGEN_TZ)
    end_aware = end_dt.replace(tzinfo=COPENHAGEN_TZ)

    student_name = get_student_name(db, conversation.phone_number)
    appt_display = APPT_TYPE_NAMES_DA.get(appt_type_name, appt_type_name)
    title_who = f"{student_name} - {conversation.phone_number}" if student_name else conversation.phone_number
    sms_who = f"{student_name} ({conversation.phone_number})" if student_name else conversation.phone_number

    # 999.37 pilot seed — surface the student's lektionsplan position on the
    # calendar event title and the owner CC so the owner sees where they are on every
    # booking. No-op when unknown (null / "ved ikke").
    lesson = get_student_lesson(db, conversation.phone_number)
    lesson_suffix = f" – lektion {lesson}" if lesson else ""

    try:
        service = build_calendar_service()
        event = create_event(
            service,
            GCAL_CALENDAR_ID,
            summary=f"{appt_display} - {title_who}{lesson_suffix}",
            start_dt=start_aware,
            end_dt=end_aware,
        )
        booking.gcal_event_id = event.get("id")
        # No synchronous poll_calendar() here: it's a redundant full iCloud
        # re-fetch on the webhook's critical path. The confirmed Booking row
        # already blocks this slot for the next student (slot_engine reads
        # confirmed Bookings live in get_busy_intervals), and the cache is
        # refreshed by the 15-min background poll. Removing it keeps the
        # webhook well under Twilio's ~15s timeout.
    except Exception:
        logger.exception("Failed to create calendar event for booking")
        # Fix #2: alert owner — student confirmed but instructor has no calendar entry.
        if OWNER_PHONE:
            local_time = start_aware.strftime("%d/%m kl. %H:%M")
            send_sms(
                OWNER_PHONE,
                f"[LISA FEJL] Kalender IKKE opdateret for {sms_who} {local_time}. Tilføj manuelt.",
            )

    # Format confirmation time in Copenhagen local time
    local_time = start_aware.strftime("%d/%m kl. %H:%M")

    # Clear booking context and commit before side effects (SMS).
    # This ensures the booking is persisted before notifying the owner,
    # avoiding SMS about a booking that doesn't exist if commit were to fail.
    _save_context(conversation, {})
    db.commit()

    # Notify owner (per BOOK-03) — after commit so booking is guaranteed persisted
    if OWNER_PHONE:
        send_sms(OWNER_PHONE, f"[LISA BOOKING] {sms_who} bookede {appt_display} {local_time}")
    # CC the school owner (the owner) on the booking they must act on. No-op unless
    # SCHOOL_OWNER_PHONE is set; friendlier wording (no dev-facing [LISA …] tag).
    cc_school_owner(f"Ny booking: {sms_who} – {appt_display} {local_time}{lesson_suffix}")

    # Send the booking confirmation via the Twilio API rather than embedding it
    # in the webhook's TwiML response. The heavy CalDAV work above (auth + event
    # write) can push the webhook past Twilio's ~15s timeout, in which case a
    # TwiML reply is silently dropped — which is exactly why this confirmation
    # used to go missing while the owner [LISA BOOKING] (an API send) survived.
    # Sending via API decouples delivery from the HTTP response timing.
    if language == "en":
        confirmation = f"Booked! {local_time}. See you there! You'll get a reminder the day before."
    else:
        confirmation = f"Booket! {local_time}. Vi ses! Du får en påmindelse dagen før."

    send_sms(conversation.phone_number, confirmation)

    # Log the outbound confirmation ourselves: the webhook only persists the
    # outbound row when handle_message returns a non-empty reply, and we return
    # an empty (silent) reply below to avoid double-sending the confirmation.
    db.add(
        Message(
            conversation_id=conversation.id,
            direction="outbound",
            body=confirmation,
            timestamp=datetime.now(timezone.utc),
        )
    )
    db.commit()

    # Empty reply → webhook returns silent TwiML; the confirmation was already
    # delivered via the API send above.
    return "", ConversationState.IDLE


def _format_slot(dt: datetime) -> str:
    """Format a naive Copenhagen-local datetime as compact Danish-friendly string.

    Slot engine produces naive Copenhagen times. Output like: 'tir 15/4 kl.10:00'
    """
    local = dt.replace(tzinfo=COPENHAGEN_TZ)
    day_abbrev = ["man", "tir", "ons", "tor", "fre", "lør", "søn"][local.weekday()]
    return f"{day_abbrev} {local.day}/{local.month} kl.{local.strftime('%H:%M')}"


def _set_offered_slots(context: dict, slots: list[datetime]) -> None:
    """Persist the offered-slot list in context.

    Stores the full list under offered_slots (ISO strings) AND mirrors the
    first one to offered_slot so "ja" / _confirm_booking continue to work
    against position 0 unchanged. _handle_offer_reply overwrites
    offered_slot when the student picks a different position.

    Also resets mid_flow_faq_count (Phase 999.7) — a freshly emitted offer
    is a fresh attempt; the cap-at-2 budget restarts here. This is the
    canonical "new offer state begins now" marker, called by all four
    offer-emission entry points (_offer_slot_on_date, _offer_slot_in_range,
    _offer_earliest_slot, _search_and_offer).
    """
    iso = [s.isoformat() for s in slots]
    context["booking"]["offered_slots"] = iso
    if iso:
        context["booking"]["offered_slot"] = iso[0]
    # Phase 999.7: each new offer resets the FAQ pass-through budget.
    reset_faq_count(context, "booking")


def _format_multi_offer(slots: list[datetime], language: str) -> str:
    """Render numbered "1) X  2) Y  3) Z — svar 1, 2 eller 3" SMS body.

    Caller is responsible for the lead-in (e.g. "Ledige tider: ").
    Single-slot offers should use the legacy single-line phrasing instead;
    this helper only handles the >=2 case.
    """
    parts = [f"{i+1}) {_format_slot(s)}" for i, s in enumerate(slots)]
    body = "  ".join(parts)
    if language == "en":
        suffix = " — reply 1, 2 or 3" if len(slots) >= 3 else " — reply 1 or 2"
    else:
        suffix = " — svar 1, 2 eller 3" if len(slots) >= 3 else " — svar 1 eller 2"
    return body + suffix


def _get_context(conversation: Conversation) -> dict:
    try:
        return json.loads(conversation.context_json or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}


def _save_context(conversation: Conversation, context: dict) -> None:
    conversation.context_json = json.dumps(context, ensure_ascii=False)


def _fallback(language: str) -> str:
    if language == "en":
        return "Something went wrong with the booking. Please try again."
    return "Noget gik galt med bookingen. Prøv venligst igen."
