"""Cancellation and rescheduling flow handler (D-01 through D-11)."""
import json
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.booking.preference_extractor import extract_preferences
from app.booking.slot_finder import (
    find_earliest_slot,
    find_nearest_alternative,
    find_slots_for_preferences,
)
from app.calendar.client import (
    CalendarSafetyError,
    build_calendar_service,
    create_event,
    delete_event,
)
from app.calendar.slot_engine import load_appointment_types
from app.cancellation.policy import is_within_cancellation_policy
from app.config import GCAL_CALENDAR_ID, OWNER_PHONE
from app.conversation.affirmation import is_affirmation
from app.conversation.state import ConversationState
from app.escalation.forward import forward_to_owner
from app.lead.capture import get_student_name
from app.models import Booking, Conversation, Message
from app.sms.sender import cc_school_owner, send_sms

logger = logging.getLogger(__name__)

COPENHAGEN_TZ = ZoneInfo("Europe/Copenhagen")


def _now_local() -> datetime:
    """Current wall-clock time as a naive Copenhagen-local datetime.

    Patchable seam for the reschedule-confirm past-slot guard (mirror of
    app.booking.handler._now_local) so the time-travelling test suite can freeze
    the guard's clock.
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

# Confirmation words
_CONFIRM_WORDS = {"ja", "yes", "ok", "jep", "jo", "y", "yep", "sure"}
# Rejection words
_REJECT_WORDS = {"nej", "no", "cancel", "annuller", "stop"}


def _narrow_bookings_by_message(bookings: list, body: str) -> list:
    """Filter a student's bookings by any date/time they stated in ``body``.

    Lets "aflys min tid kl 14 i morgen" resolve directly to the 14:00 booking
    instead of showing the disambiguation list. Booking.start_dt is naive
    Copenhagen local (same frame the student sees in offers), so the hour/minute
    comparison against the HH:MM ``preferred_time`` is direct. Returns the
    narrowed list; the caller only short-circuits when exactly one remains, so an
    ambiguous or empty result safely falls through to the normal list.
    """
    prefs = extract_preferences(body)
    candidates = list(bookings)

    if prefs.target_date:
        try:
            wanted = datetime.fromisoformat(prefs.target_date).date()
            candidates = [b for b in candidates if b.start_dt.date() == wanted]
        except (ValueError, TypeError):
            pass

    if prefs.preferred_time:
        try:
            hh, mm = (int(x) for x in prefs.preferred_time.split(":"))
            candidates = [
                b for b in candidates if (b.start_dt.hour, b.start_dt.minute) == (hh, mm)
            ]
        except (ValueError, TypeError):
            pass

    return candidates


def start_cancellation_flow(
    db: Session,
    conversation: Conversation,
    body: str,
    language: str,
    intent: str,
    target_booking_id: int | None = None,
) -> tuple[str, ConversationState]:
    """Entry point: cancel_booking or reschedule_booking intent detected in IDLE state.

    Per D-01:
    - Look up confirmed upcoming bookings for this phone number.
    - If none: inform student.
    - If one: go straight to action (cancel confirm or reschedule slot offer).
    - If many: show disambiguation list (max 3 soonest) and ask which.

    ``target_booking_id`` (reminder fast-path): when a student replies NEJ /
    reschedule to a day-before reminder, the reminder already named one specific
    booking. Act on it directly instead of re-asking which one. Falls through to
    the normal lookup if that booking is gone (already cancelled / now past).

    SECURITY (T-08-01): always filter by phone_number == conversation.phone_number.
    """
    now = datetime.now(COPENHAGEN_TZ).replace(tzinfo=None)

    if target_booking_id is not None:
        booking = (
            db.query(Booking)
            .filter(
                Booking.id == target_booking_id,
                Booking.phone_number == conversation.phone_number,
                Booking.start_dt > now,
                Booking.status == "confirmed",
            )
            .first()
        )
        if booking is not None:
            context: dict = {
                "cancellation": {
                    "intent": intent,
                    "booking_id": booking.id,
                    "appointment_type": booking.appointment_type,
                }
            }
            if intent == "reschedule_booking":
                booking.status = "reschedule_pending"
                db.flush()
                _save_context(conversation, context)
                return _start_reschedule_slot_selection(
                    db, conversation, booking, language, context
                )
            _save_context(conversation, context)
            return _ask_cancel_confirm(booking, language)

    bookings = (
        db.query(Booking)
        .filter(
            Booking.phone_number == conversation.phone_number,
            Booking.start_dt > now,
            Booking.status == "confirmed",
        )
        .order_by(Booking.start_dt)
        .limit(3)
        .all()
    )

    if not bookings:
        if language == "en":
            return "You have no upcoming confirmed bookings.", ConversationState.IDLE
        return "Du har ingen kommende bookinger.", ConversationState.IDLE

    # Natural-language narrowing: the student may already have named the slot
    # ("aflys min tid kl 14 i morgen"). If their words uniquely identify one of
    # their bookings, skip the "hvilken booking?" list. Falls through to the list
    # when the words are ambiguous or match nothing.
    if len(bookings) > 1:
        narrowed = _narrow_bookings_by_message(bookings, body)
        if len(narrowed) == 1:
            bookings = narrowed

    if len(bookings) == 1:
        booking = bookings[0]
        context: dict = {
            "cancellation": {
                "intent": intent,
                "booking_id": booking.id,
                "appointment_type": booking.appointment_type,
            }
        }
        if intent == "reschedule_booking":
            # Mark as reschedule_pending per D-09
            booking.status = "reschedule_pending"
            db.flush()
            _save_context(conversation, context)
            return _start_reschedule_slot_selection(db, conversation, booking, language, context)
        else:
            # cancel_booking: ask for confirmation
            _save_context(conversation, context)
            return _ask_cancel_confirm(booking, language)

    # Multiple bookings: show disambiguation list (T-08-03: only this student's bookings)
    context = {
        "cancellation": {
            "intent": intent,
            "candidate_ids": [b.id for b in bookings],
        }
    }
    _save_context(conversation, context)
    lines = []
    for i, b in enumerate(bookings, start=1):
        type_name = APPT_TYPE_NAMES_DA.get(b.appointment_type, b.appointment_type)
        formatted = _format_slot(b.start_dt)
        lines.append(f"{i}. {type_name} {formatted}")
    booking_list = "\n".join(lines)
    if language == "en":
        type_names_list = [
            APPT_TYPE_NAMES_EN.get(b.appointment_type, b.appointment_type) for b in bookings
        ]
        lines_en = [
            f"{i}. {name} {_format_slot(b.start_dt)}"
            for i, (name, b) in enumerate(zip(type_names_list, bookings), start=1)
        ]
        return (
            f"Which booking? Reply with a number:\n{chr(10).join(lines_en)}",
            ConversationState.CANCEL_CONFIRM,
        )
    return (
        f"Hvilken booking? Svar med et nummer:\n{booking_list}",
        ConversationState.CANCEL_CONFIRM,
    )


def handle_cancellation_step(
    db: Session,
    conversation: Conversation,
    body: str,
    language: str,
) -> tuple[str, ConversationState]:
    """Dispatch based on current conversation state within the cancellation flow."""
    state = ConversationState(conversation.state)
    context = _get_context(conversation)

    if state == ConversationState.CANCEL_CONFIRM:
        return _handle_cancel_confirm(db, conversation, body, language, context)
    if state == ConversationState.RESCHEDULE_SELECT_SLOT:
        return _handle_reschedule_slot(db, conversation, body, language, context)
    if state == ConversationState.RESCHEDULE_CONFIRM:
        return _handle_reschedule_confirm(db, conversation, body, language, context)

    # Unexpected state — revert any reschedule_pending and reset
    cancel_ctx = context.get("cancellation", {})
    booking_id = cancel_ctx.get("booking_id")
    if booking_id:
        revert_reschedule_pending(db, booking_id, conversation.phone_number)
    _save_context(conversation, {})
    return _fallback(language), ConversationState.IDLE


def revert_reschedule_pending(db: Session, booking_id: int, phone_number: str) -> None:
    """Revert a booking from reschedule_pending back to confirmed (D-11).

    Called when student abandons mid-reschedule or on unexpected state reset.
    SECURITY (T-08-01): filters by phone_number to prevent cross-student tampering.
    """
    booking = (
        db.query(Booking)
        .filter(
            Booking.id == booking_id,
            Booking.phone_number == phone_number,
            Booking.status == "reschedule_pending",
        )
        .first()
    )
    if booking:
        booking.status = "confirmed"
        db.commit()


def _ask_cancel_confirm(booking: Booking, language: str) -> tuple[str, ConversationState]:
    """Format a cancellation confirmation request showing booking details.

    Includes the refund implication so the student sees the cost (full
    refund vs. no refund) before confirming, not after.
    """
    formatted = _format_slot(booking.start_dt)
    type_name = APPT_TYPE_NAMES_DA.get(booking.appointment_type, booking.appointment_type)
    within_policy = is_within_cancellation_policy(booking.start_dt, booking.appointment_type)
    if language == "en":
        type_name_en = APPT_TYPE_NAMES_EN.get(booking.appointment_type, booking.appointment_type)
        notice = (
            "Within the cancellation window — full refund."
            if within_policy
            else "NOTE: past the deadline — no refund."
        )
        return (
            f"Cancel your {type_name_en} on {formatted}? {notice} Reply 'yes' or 'no'.",
            ConversationState.CANCEL_CONFIRM,
        )
    notice_da = (
        "Du er inden for fristen — fuld refusion."
        if within_policy
        else "OBS: for sent — ingen refusion."
    )
    return (
        f"Vil du aflyse din {type_name} {formatted}? {notice_da} Svar 'ja' eller 'nej'.",
        ConversationState.CANCEL_CONFIRM,
    )


def _handle_cancel_confirm(
    db: Session,
    conversation: Conversation,
    body: str,
    language: str,
    context: dict,
) -> tuple[str, ConversationState]:
    """Handle confirmation step for cancellation.

    Supports both single-booking (booking_id in context) and multi-booking
    disambiguation (candidate_ids in context).
    """
    cancel_ctx = context.get("cancellation", {})
    candidate_ids = cancel_ctx.get("candidate_ids")
    intent = cancel_ctx.get("intent", "cancel_booking")

    # --- Multi-booking disambiguation ---
    if candidate_ids:
        body_stripped = body.strip()
        selected_id = None

        # Try parsing as a number
        if body_stripped.isdigit():
            idx = int(body_stripped) - 1
            if 0 <= idx < len(candidate_ids):
                selected_id = candidate_ids[idx]

        # Try parsing as appointment type keyword (via extract_preferences)
        if selected_id is None:
            prefs = extract_preferences(body)
            if prefs.appointment_type:
                # Find the first candidate with this type
                matching = (
                    db.query(Booking)
                    .filter(
                        Booking.id.in_(candidate_ids),
                        Booking.phone_number == conversation.phone_number,
                        Booking.appointment_type == prefs.appointment_type,
                    )
                    .first()
                )
                if matching:
                    selected_id = matching.id

        if selected_id is None:
            # Could not disambiguate — track attempts
            attempts = cancel_ctx.get("disambig_attempts", 0) + 1
            cancel_ctx["disambig_attempts"] = attempts
            context["cancellation"] = cancel_ctx
            if attempts >= 3:
                _save_context(conversation, {})
                return (
                    forward_to_owner(db, conversation, body, reason="cancel_disambig_3x", language=language),
                    ConversationState.IDLE,
                )
            _save_context(conversation, context)
            if language == "en":
                return (
                    "Please reply with the number of the booking you want to cancel (1, 2, or 3).",
                    ConversationState.CANCEL_CONFIRM,
                )
            return (
                "Svar venligst med nummeret på den booking du vil aflyse (1, 2 eller 3).",
                ConversationState.CANCEL_CONFIRM,
            )

        # Found the booking — load it and ask for confirmation
        booking = (
            db.query(Booking)
            .filter(
                Booking.id == selected_id,
                Booking.phone_number == conversation.phone_number,
            )
            .first()
        )
        if not booking:
            _save_context(conversation, {})
            return _fallback(language), ConversationState.IDLE

        cancel_ctx = {
            "intent": intent,
            "booking_id": booking.id,
            "appointment_type": booking.appointment_type,
        }
        context["cancellation"] = cancel_ctx

        if intent == "reschedule_booking":
            booking.status = "reschedule_pending"
            db.flush()
            _save_context(conversation, context)
            return _start_reschedule_slot_selection(db, conversation, booking, language, context)
        else:
            _save_context(conversation, context)
            return _ask_cancel_confirm(booking, language)

    # --- Single booking confirmation ---
    booking_id = cancel_ctx.get("booking_id")
    if not booking_id:
        _save_context(conversation, {})
        return _fallback(language), ConversationState.IDLE

    body_lower = body.strip().lower()

    # Rejection
    if body_lower in _REJECT_WORDS:
        revert_reschedule_pending(db, booking_id, conversation.phone_number)
        _save_context(conversation, {})
        if language == "en":
            return "OK, your booking is kept.", ConversationState.IDLE
        return "OK, din booking er bibeholdt.", ConversationState.IDLE

    # Confirmation
    if is_affirmation(body):
        booking = (
            db.query(Booking)
            .filter(
                Booking.id == booking_id,
                Booking.phone_number == conversation.phone_number,
            )
            .first()
        )
        if not booking:
            _save_context(conversation, {})
            return _fallback(language), ConversationState.IDLE

        within_policy = is_within_cancellation_policy(
            booking.start_dt, booking.appointment_type
        )

        now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
        booking.status = "cancelled"
        booking.cancelled_at = now_utc

        _save_context(conversation, {})
        db.commit()

        # Best-effort: delete calendar event.
        # No poll_calendar() here: the cancelled Booking row already frees the
        # slot for the slot engine (get_busy_intervals reads confirmed Bookings
        # live), and the 15-min background poll clears the stale cache entry.
        # Keeping a synchronous iCloud re-fetch on the webhook path risked the
        # ~15s timeout that drops the confirmation SMS.
        if booking.gcal_event_id:
            try:
                service = build_calendar_service()
                delete_event(service, GCAL_CALENDAR_ID, booking.gcal_event_id)
            except CalendarSafetyError:
                # The guard refused: this URL is outside the Lisa collection, so
                # the row's gcal_event_id is wrong, not the calendar. Distinct
                # from a transient backend failure below — that one retries next
                # cancellation, this one never will and needs a human.
                logger.error(
                    "SAFETY: refused to delete calendar event %s for booking %s — outside "
                    "the Lisa collection. The booking is cancelled in the DB; the calendar "
                    "event is still there and must be removed by hand.",
                    booking.gcal_event_id,
                    booking.id,
                )
            except Exception:
                logger.exception("Failed to delete calendar event %s", booking.gcal_event_id)

        # Notify instructor per D-08
        if OWNER_PHONE:
            type_name_da = APPT_TYPE_NAMES_DA.get(booking.appointment_type, booking.appointment_type)
            formatted = _format_slot(booking.start_dt)
            student_name = get_student_name(db, conversation.phone_number)
            sms_who = f"{student_name} ({conversation.phone_number})" if student_name else conversation.phone_number
            send_sms(
                OWNER_PHONE,
                f"[LISA AFLYST] {sms_who} aflysning: {type_name_da} {formatted}",
            )
            # CC the school owner (the owner) on the cancellation. No-op unless
            # SCHOOL_OWNER_PHONE is set; friendlier wording (no dev-facing tag).
            cc_school_owner(f"Aflysning: {sms_who} – {type_name_da} {formatted}")

        type_name_da = APPT_TYPE_NAMES_DA.get(booking.appointment_type, booking.appointment_type)
        formatted = _format_slot(booking.start_dt)

        if within_policy:
            if language == "en":
                type_name_en = APPT_TYPE_NAMES_EN.get(booking.appointment_type, booking.appointment_type)
                confirmation = f"Your {type_name_en} on {formatted} has been cancelled. Hope to see you again soon."
            else:
                confirmation = f"Din {type_name_da} {formatted} er aflyst. Vi håber at se dig igen snart."
        else:
            # Late cancellation per D-05: warn but do not block
            if language == "en":
                confirmation = "Your cancellation has been registered. Note: you are cancelling within the notice period and will not receive a refund."
            else:
                confirmation = "Din aflysning er registreret. Bemærk: du aflyser inden for aflysningsfristen og vil ikke få refusion."

        return _deliver_confirmation(db, conversation, confirmation), ConversationState.IDLE

    # Unrecognized response — re-prompt
    attempts = cancel_ctx.get("confirm_attempts", 0) + 1
    cancel_ctx["confirm_attempts"] = attempts
    context["cancellation"] = cancel_ctx
    if attempts >= 3:
        revert_reschedule_pending(db, booking_id, conversation.phone_number)
        _save_context(conversation, {})
        return (
            forward_to_owner(db, conversation, body, reason="cancel_confirm_3x", language=language),
            ConversationState.IDLE,
        )
    _save_context(conversation, context)
    if language == "en":
        return (
            "Reply 'yes' to confirm cancellation or 'no' to keep your booking.",
            ConversationState.CANCEL_CONFIRM,
        )
    return (
        "Svar 'ja' for at bekræfte aflysning eller 'nej' for at beholde din tid.",
        ConversationState.CANCEL_CONFIRM,
    )


def _start_reschedule_slot_selection(
    db: Session,
    conversation: Conversation,
    booking: Booking,
    language: str,
    context: dict,
) -> tuple[str, ConversationState]:
    """Offer the earliest available slot of the same appointment type for rescheduling."""
    slot = find_earliest_slot(booking.appointment_type)
    if not slot:
        # No slots available — revert to confirmed per D-11
        revert_reschedule_pending(db, booking.id, conversation.phone_number)
        _save_context(conversation, {})
        if language == "en":
            return (
                "Unfortunately no available slots right now. Your current booking is kept.",
                ConversationState.IDLE,
            )
        return (
            "Desværre ingen ledige tider. Din nuværende booking er bevaret.",
            ConversationState.IDLE,
        )

    cancel_ctx = context.get("cancellation", {})
    cancel_ctx["offered_slot"] = slot.isoformat()
    context["cancellation"] = cancel_ctx
    _save_context(conversation, context)

    formatted = _format_slot(slot)
    old_formatted = _format_slot(booking.start_dt)
    if language == "en":
        return (
            f"How about {formatted}? Reply 'yes' to confirm the change from {old_formatted}, or write your preferred days.",
            ConversationState.RESCHEDULE_SELECT_SLOT,
        )
    return (
        f"Hvad med {formatted}? Svar 'ja' for at bekræfte ændring fra {old_formatted}, eller skriv dine ønsker.",
        ConversationState.RESCHEDULE_SELECT_SLOT,
    )


def _handle_reschedule_slot(
    db: Session,
    conversation: Conversation,
    body: str,
    language: str,
    context: dict,
) -> tuple[str, ConversationState]:
    """Handle student's response to slot offer during rescheduling."""
    cancel_ctx = context.get("cancellation", {})
    booking_id = cancel_ctx.get("booking_id")
    body_lower = body.strip().lower()

    if not booking_id:
        _save_context(conversation, {})
        return _fallback(language), ConversationState.IDLE

    # Confirmation of offered slot
    if is_affirmation(body):
        offered_slot_str = cancel_ctx.get("offered_slot")
        if not offered_slot_str:
            _save_context(conversation, {})
            return _fallback(language), ConversationState.IDLE
        new_slot = datetime.fromisoformat(offered_slot_str)
        old_booking = (
            db.query(Booking)
            .filter(
                Booking.id == booking_id,
                Booking.phone_number == conversation.phone_number,
            )
            .first()
        )
        old_formatted = _format_slot(old_booking.start_dt) if old_booking else "?"
        new_formatted = _format_slot(new_slot)
        if language == "en":
            return (
                f"Confirm change: from {old_formatted} to {new_formatted}? Reply 'yes' or 'no'.",
                ConversationState.RESCHEDULE_CONFIRM,
            )
        return (
            f"Bekræft ændring: fra {old_formatted} til {new_formatted}? Svar 'ja' eller 'nej'.",
            ConversationState.RESCHEDULE_CONFIRM,
        )

    # Rejection — revert and restore booking
    if body_lower in _REJECT_WORDS:
        revert_reschedule_pending(db, booking_id, conversation.phone_number)
        _save_context(conversation, {})
        if language == "en":
            return "OK, your current booking is kept.", ConversationState.IDLE
        return "OK, din nuværende booking er bevaret.", ConversationState.IDLE

    # Try to extract day/time preferences for alternative slot search
    prefs = extract_preferences(body)
    if prefs.days or prefs.time_of_day or prefs.preferred_time:
        cancel_ctx["preferred_days"] = prefs.days or []
        if prefs.time_of_day:
            cancel_ctx["time_of_day"] = prefs.time_of_day
        if prefs.preferred_time:
            cancel_ctx["preferred_time"] = prefs.preferred_time
        context["cancellation"] = cancel_ctx
        _save_context(conversation, context)
        return _reschedule_search_and_offer(db, conversation, language, context)

    # Could not parse — track attempts
    attempts = cancel_ctx.get("slot_attempts", 0) + 1
    cancel_ctx["slot_attempts"] = attempts
    context["cancellation"] = cancel_ctx
    if attempts >= 3:
        revert_reschedule_pending(db, booking_id, conversation.phone_number)
        _save_context(conversation, {})
        return (
            forward_to_owner(db, conversation, body, reason="reschedule_slot_3x", language=language),
            ConversationState.IDLE,
        )
    _save_context(conversation, context)
    if language == "en":
        return (
            "Reply 'yes' to confirm, or write 2-3 preferred days.",
            ConversationState.RESCHEDULE_SELECT_SLOT,
        )
    return (
        "Svar 'ja' for at bekræfte, eller skriv 2-3 dage der passer dig.",
        ConversationState.RESCHEDULE_SELECT_SLOT,
    )


def _reschedule_search_and_offer(
    db: Session,
    conversation: Conversation,
    language: str,
    context: dict,
) -> tuple[str, ConversationState]:
    """Find slots matching preferences for reschedule, offer best fit or nearest alternative."""
    cancel_ctx = context.get("cancellation", {})
    appt_type = cancel_ctx.get("appointment_type", "lesson")
    days = cancel_ctx.get("preferred_days", [])
    time_of_day = cancel_ctx.get("time_of_day")
    preferred_time = cancel_ctx.get("preferred_time")
    booking_id = cancel_ctx.get("booking_id")

    slots = find_slots_for_preferences(appt_type, days, time_of_day, preferred_time=preferred_time)
    if slots:
        best = slots[0]
        cancel_ctx["offered_slot"] = best.isoformat()
        context["cancellation"] = cancel_ctx
        _save_context(conversation, context)
        formatted = _format_slot(best)
        if language == "en":
            return (
                f"How about {formatted}? Reply 'yes' to confirm or write other days.",
                ConversationState.RESCHEDULE_SELECT_SLOT,
            )
        return (
            f"Hvad med {formatted}? Svar 'ja' for at bekræfte eller skriv andre dage.",
            ConversationState.RESCHEDULE_SELECT_SLOT,
        )

    # No matching slots — suggest nearest alternative
    alternative = find_nearest_alternative(appt_type)
    if alternative:
        cancel_ctx["offered_slot"] = alternative.isoformat()
        cancel_ctx.pop("preferred_days", None)
        cancel_ctx.pop("time_of_day", None)
        context["cancellation"] = cancel_ctx
        _save_context(conversation, context)
        formatted = _format_slot(alternative)
        if language == "en":
            return (
                f"No slots for your preference. Nearest: {formatted}. OK? Or write other days.",
                ConversationState.RESCHEDULE_SELECT_SLOT,
            )
        return (
            f"Ingen ledige tider der. Nærmeste: {formatted}. Passer det? Ellers skriv andre dage.",
            ConversationState.RESCHEDULE_SELECT_SLOT,
        )

    # No slots at all — revert
    if booking_id:
        revert_reschedule_pending(db, booking_id, conversation.phone_number)
    _save_context(conversation, {})
    if language == "en":
        return "Sorry, no available slots right now. Your current booking is kept.", ConversationState.IDLE
    return "Desværre ingen ledige tider. Din nuværende booking er bevaret.", ConversationState.IDLE


def _handle_reschedule_confirm(
    db: Session,
    conversation: Conversation,
    body: str,
    language: str,
    context: dict,
) -> tuple[str, ConversationState]:
    """Atomic commit of reschedule: cancel old booking, create new one (D-10)."""
    cancel_ctx = context.get("cancellation", {})
    booking_id = cancel_ctx.get("booking_id")
    offered_slot_str = cancel_ctx.get("offered_slot")
    appt_type = cancel_ctx.get("appointment_type", "lesson")
    body_lower = body.strip().lower()

    if not booking_id or not offered_slot_str:
        _save_context(conversation, {})
        return _fallback(language), ConversationState.IDLE

    # Rejection — revert
    if body_lower in _REJECT_WORDS:
        revert_reschedule_pending(db, booking_id, conversation.phone_number)
        _save_context(conversation, {})
        if language == "en":
            return "OK, your current booking is kept.", ConversationState.IDLE
        return "OK, din nuværende booking er bevaret.", ConversationState.IDLE

    # Confirmation
    if is_affirmation(body):
        old_booking = (
            db.query(Booking)
            .filter(
                Booking.id == booking_id,
                Booking.phone_number == conversation.phone_number,
            )
            .first()
        )
        if not old_booking:
            _save_context(conversation, {})
            return _fallback(language), ConversationState.IDLE

        new_slot = datetime.fromisoformat(offered_slot_str)
        appt_types = load_appointment_types()
        at = appt_types.get(appt_type)
        if not at:
            _save_context(conversation, {})
            return _fallback(language), ConversationState.IDLE

        new_end = new_slot + timedelta(minutes=at.duration_minutes)

        # Finding 1 (booking audit) — past-slot guard on the rescheduled slot.
        # Mirror of the booking-confirm guard: if the new slot has since passed
        # (student delayed the final "ja"), keep the original booking intact.
        if new_slot <= _now_local():
            revert_reschedule_pending(db, booking_id, conversation.phone_number)
            _save_context(conversation, {})
            if language == "en":
                return (
                    "Sorry, that new time has now passed. Your current booking is kept.",
                    ConversationState.IDLE,
                )
            return (
                "Den nye tid er desværre passeret. Din nuværende booking er bevaret.",
                ConversationState.IDLE,
            )

        old_formatted = _format_slot(old_booking.start_dt)
        new_formatted = _format_slot(new_slot)
        old_gcal_event_id = old_booking.gcal_event_id

        # Atomic DB commit (D-10): cancel old, create new.
        # If the new slot was just taken by another student, the partial unique
        # index on bookings(start_dt, end_dt) WHERE status='confirmed' raises
        # IntegrityError on flush. Roll back the in-memory cancel of old_booking
        # and revert it back to confirmed so the student keeps their original
        # slot. (revert_reschedule_pending requires a fresh transaction since
        # the rollback detaches the old_booking instance.)
        now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
        old_booking.status = "cancelled"
        old_booking.cancelled_at = now_utc

        new_booking = Booking(
            phone_number=conversation.phone_number,
            conversation_id=conversation.id,
            appointment_type=appt_type,
            start_dt=new_slot,
            end_dt=new_end,
            gcal_event_id=None,
            status="confirmed",
        )
        db.add(new_booking)
        try:
            db.flush()
        except IntegrityError:
            db.rollback()
            revert_reschedule_pending(db, booking_id, conversation.phone_number)
            _save_context(conversation, {})
            if language == "en":
                return (
                    "Sorry, that new slot was just taken. Your current booking is kept.",
                    ConversationState.IDLE,
                )
            return (
                "Den nye tid er desværre netop taget. Din nuværende booking er bevaret.",
                ConversationState.IDLE,
            )

        _save_context(conversation, {})
        db.commit()

        student_name = get_student_name(db, conversation.phone_number)
        title_who = f"{student_name} - {conversation.phone_number}" if student_name else conversation.phone_number
        sms_who = f"{student_name} ({conversation.phone_number})" if student_name else conversation.phone_number

        # Fix #5: create new event BEFORE deleting old — if create fails, old event
        # is preserved in GCal so the instructor's calendar is never left with a gap.
        gcal_event_id = None
        try:
            service = build_calendar_service()
            start_aware = new_slot.replace(tzinfo=COPENHAGEN_TZ)
            end_aware = new_end.replace(tzinfo=COPENHAGEN_TZ)
            appt_display = APPT_TYPE_NAMES_DA.get(appt_type, appt_type)
            event = create_event(
                service,
                GCAL_CALENDAR_ID,
                summary=f"{appt_display} - {title_who}",
                start_dt=start_aware,
                end_dt=end_aware,
            )
            gcal_event_id = event.get("id")
            if gcal_event_id:
                new_booking.gcal_event_id = gcal_event_id
                db.commit()
            # No poll_calendar() here: the new confirmed Booking row already
            # blocks the new slot (get_busy_intervals reads confirmed Bookings
            # live) and the freed old slot clears on the 15-min background poll.
            # A synchronous iCloud re-fetch risked the ~15s webhook timeout that
            # drops the confirmation SMS (reschedule is the heaviest flow).
        except Exception:
            logger.exception("Failed to create new calendar event for reschedule")
            # Fix #2: alert owner — student confirmed reschedule but instructor has no new calendar entry.
            if OWNER_PHONE:
                send_sms(
                    OWNER_PHONE,
                    f"[LISA FEJL] Kalender IKKE opdateret for omlægning {sms_who} → {new_formatted}. Tilføj manuelt.",
                )

        # Only delete old event after new one is confirmed — avoids a gap in the calendar.
        if old_gcal_event_id and gcal_event_id:
            try:
                service = build_calendar_service()
                delete_event(service, GCAL_CALENDAR_ID, old_gcal_event_id)
            except CalendarSafetyError:
                # Worse here than on the cancel path: the new event is already
                # written, so a refusal leaves the student double-booked in the
                # calendar until someone removes the old one by hand.
                logger.error(
                    "SAFETY: refused to delete old calendar event %s during reschedule — "
                    "outside the Lisa collection. The new event %s IS created, so the "
                    "calendar now holds BOTH; remove the old one by hand.",
                    old_gcal_event_id,
                    gcal_event_id,
                )
            except Exception:
                logger.exception("Failed to delete old calendar event %s", old_gcal_event_id)

        # Notify instructor per D-08
        if OWNER_PHONE:
            type_name_da = APPT_TYPE_NAMES_DA.get(appt_type, appt_type)
            send_sms(
                OWNER_PHONE,
                f"[LISA ÆNDRET] Elev {sms_who} ændret fra {old_formatted} til {new_formatted}. {type_name_da}",
            )

        if language == "en":
            confirmation = f"Change confirmed. Your new appointment is {new_formatted}. See you then!"
        else:
            confirmation = f"Ændringen er registreret. Din nye tid er {new_formatted}. Vi ses!"

        return _deliver_confirmation(db, conversation, confirmation), ConversationState.IDLE

    # Unrecognized response — re-prompt
    attempts = cancel_ctx.get("reschedule_confirm_attempts", 0) + 1
    cancel_ctx["reschedule_confirm_attempts"] = attempts
    context["cancellation"] = cancel_ctx
    if attempts >= 3:
        revert_reschedule_pending(db, booking_id, conversation.phone_number)
        _save_context(conversation, {})
        return (
            forward_to_owner(db, conversation, body, reason="reschedule_confirm_3x", language=language),
            ConversationState.IDLE,
        )
    _save_context(conversation, context)
    if language == "en":
        return (
            "Reply 'yes' to confirm the change or 'no' to keep your current booking.",
            ConversationState.RESCHEDULE_CONFIRM,
        )
    return (
        "Svar 'ja' for at bekræfte ændringen eller 'nej' for at beholde din nuværende tid.",
        ConversationState.RESCHEDULE_CONFIRM,
    )


def _format_slot(dt: datetime) -> str:
    """Format a naive Copenhagen-local datetime as compact Danish-friendly string.

    Slot engine produces naive Copenhagen times. Output like: 'tir 15/4 kl.10:00'
    """
    local = dt.replace(tzinfo=COPENHAGEN_TZ)
    day_abbrev = ["man", "tir", "ons", "tor", "fre", "lør", "søn"][local.weekday()]
    return f"{day_abbrev} {local.day}/{local.month} kl.{local.strftime('%H:%M')}"


def _deliver_confirmation(db: Session, conversation: Conversation, text: str) -> str:
    """Send a flow confirmation via the Twilio API and log it as an outbound row.

    Cancellation and reschedule confirmations used to ride the webhook's TwiML
    response, so Twilio only delivered them if the (CalDAV-heavy) webhook
    returned within ~15s — the same timeout that dropped the booking "Booket!"
    SMS. Sending via the API instead decouples delivery from the HTTP response.
    Returns "" so the webhook emits a silent TwiML reply (no double-send); the
    outbound is logged here because the webhook only persists non-empty replies.
    """
    send_sms(conversation.phone_number, text)
    db.add(
        Message(
            conversation_id=conversation.id,
            direction="outbound",
            body=text,
            timestamp=datetime.now(timezone.utc),
        )
    )
    db.commit()
    return ""


def _get_context(conversation: Conversation) -> dict:
    try:
        return json.loads(conversation.context_json or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}


def _save_context(conversation: Conversation, context: dict) -> None:
    conversation.context_json = json.dumps(context, ensure_ascii=False)


def _fallback(language: str) -> str:
    if language == "en":
        return "Something went wrong. Please try again."
    return "Noget gik galt. Prøv venligst igen."
