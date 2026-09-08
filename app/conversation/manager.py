import json
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.config import APPOINTMENT_TYPES, OWNER_PHONE
from app.conversation.state import (
    ALWAYS_ESCALATE_INTENTS,
    ConversationState,
    contains_urgency_keyword,
)
from app.intent.classifier import ClassificationResult, classify_message
from app.booking.handler import (
    handle_booking_status,
    handle_booking_step,
    start_booking_flow,
)
from app.cancellation.handler import handle_cancellation_step, start_cancellation_flow
from app.lead.capture import handle_lead_step
from app.models import Booking, Conversation, Escalation, Message
from app.response.generator import generate_faq_response, generate_response
from app.response.templates import get_template
from app.sms.sender import send_sms

logger = logging.getLogger(__name__)

COPENHAGEN_TZ = ZoneInfo("Europe/Copenhagen")

BOOKING_FLOW_STATES = {
    ConversationState.BOOKING_INTAKE_NAME,
    ConversationState.BOOKING_INTAKE_LESSON,    # 999.37
    ConversationState.BOOKING_CONFIRM_LESSON,   # 999.37
    ConversationState.BOOKING_SELECT_TYPE,
    ConversationState.BOOKING_OFFER_SLOT,
    ConversationState.BOOKING_SELECT_DAY,
    ConversationState.BOOKING_SELECT_TIME,
}

CANCELLATION_FLOW_STATES = {
    ConversationState.CANCEL_CONFIRM,
    ConversationState.RESCHEDULE_SELECT_SLOT,
    ConversationState.RESCHEDULE_CONFIRM,
}


def handle_message(db: Session, conversation: Conversation, body: str) -> str | None:
    """Process an inbound message and return the response text (or None if rate-limited)."""

    # Check for urgency keywords first (whole-word match — see state.py).
    if contains_urgency_keyword(body):
        logger.info("Urgency keyword detected, escalating immediately")
        return _escalate(db, conversation, body, reason="urgency_keyword")

    # Get recent conversation context (last 10 messages)
    context = _get_conversation_context(db, conversation.id)

    # Route based on current conversation state
    state = ConversationState(conversation.state)

    if state == ConversationState.AWAITING_CLARIFICATION:
        return _handle_clarification_attempt(db, conversation, body, context)

    if state in (
        ConversationState.LEAD_INTAKE_NAME,
        ConversationState.LEAD_INTAKE_LICENSE,
        ConversationState.LEAD_INTAKE_DATE,
    ):
        language = _detect_language_from_history(db, conversation.id)
        response, new_state = handle_lead_step(db, conversation, body, language)
        _update_state(db, conversation, new_state)
        return response

    if state in BOOKING_FLOW_STATES:
        language = _detect_language_from_history(db, conversation.id)
        response, new_state = handle_booking_step(db, conversation, body, language)
        _update_state(db, conversation, new_state)
        return response

    if state in CANCELLATION_FLOW_STATES:
        language = _detect_language_from_history(db, conversation.id)
        response, new_state = handle_cancellation_step(db, conversation, body, language)
        _update_state(db, conversation, new_state)
        return response

    if state == ConversationState.AWAITING_CONFIRMATION:
        # Student replying to a reminder — classify to see if it's a confirmation
        result = classify_message(body, context)
        return _handle_reminder_reply(db, conversation, body, result, context)

    # Default: IDLE state — classify and route
    result = classify_message(body, context)
    return _route_by_intent(db, conversation, body, result, context)


def _route_by_intent(
    db: Session,
    conversation: Conversation,
    body: str,
    result: ClassificationResult,
    context: str,
) -> str | None:
    """Route a classified message to the appropriate handler."""

    # Log classification on the most recent inbound message
    _update_last_message_classification(db, conversation.id, result)

    intent = result.intent
    language = result.language

    # Intents that always escalate
    if intent in ALWAYS_ESCALATE_INTENTS:
        # Send acknowledgment to student, then escalate to owner
        response = get_template(intent, language)
        _escalate(db, conversation, body, reason=f"intent:{intent}", notify_student=False)
        return response

    # Cancel or reschedule booking -- start cancellation flow
    if intent in ("cancel_booking", "reschedule_booking"):
        response, new_state = start_cancellation_flow(db, conversation, body, language, intent)
        _update_state(db, conversation, new_state)
        return response

    # Book lesson — start booking flow
    if intent == "book_lesson":
        response, new_state = start_booking_flow(db, conversation, body, language)
        _update_state(db, conversation, new_state)
        return response

    # Booking status — answer "when is my next køretime?" from the Booking table
    if intent == "booking_status":
        return handle_booking_status(db, conversation, language)

    # FAQ question — answer from config FAQ entries (per D-10, D-15)
    if intent == "faq_question":
        return generate_faq_response(language, body, context)

    # Unknown intent
    if intent == "unknown":
        # Phase 999.35: a purely emotional opener (nervousness/anxiety) lands as
        # unknown. Don't dead-end it with a flat "I didn't understand" — give a
        # warm acknowledgment and loop in the human instructor.
        if result.emotional:
            return _handle_emotional(db, conversation, body, language)
        return _handle_unknown(db, conversation, body, language)

    # New lead inquiry — start lead capture (Phase 3 will expand this)
    if intent == "new_lead_inquiry":
        _update_state(db, conversation, ConversationState.LEAD_INTAKE_NAME)

    # Generate response
    return generate_response(intent, language, body, context)


_EMOTIONAL_ACK_DA = (
    "Det er helt normalt at være nervøs — sådan har rigtig mange elever haft det, "
    "og din kørelærer er vant til at hjælpe dem trygt videre i deres eget tempo. "
    "Jeg giver besked til skolen, så I kan tage en snak om det. Du er i gode hænder."
)
_EMOTIONAL_ACK_EN = (
    "It's completely normal to feel nervous — many students have felt exactly the "
    "same, and your instructor is used to helping them move forward at their own pace. "
    "I'll let the school know so they can have a chat with you. You're in good hands."
)


def _handle_emotional(
    db: Session,
    conversation: Conversation,
    body: str,
    language: str,
) -> str:
    """Phase 999.35: acknowledge an emotional message and loop in the human.

    Lisa does not counsel — handling a nervous student is the instructor's job —
    but she must never leave an emotional opener with a flat "jeg forstod ikke".
    Acknowledge warmly (referencing the instructor's experience), then escalate
    to the owner so a human follows up. Escalation is tagged distinctly so the
    owner can tell it apart from a "Lisa was confused" escalation.
    """
    _escalate(
        db,
        conversation,
        body,
        reason="emotional_distress",
        notify_student=False,
        owner_prefix="[LISA - ELEV USIKKER]",
    )
    return _EMOTIONAL_ACK_EN if language == "en" else _EMOTIONAL_ACK_DA


def _handle_unknown(
    db: Session,
    conversation: Conversation,
    body: str,
    language: str,
) -> str:
    """Handle unknown intent — ask for clarification or escalate."""
    state = ConversationState(conversation.state)

    if state == ConversationState.AWAITING_CLARIFICATION:
        # Second unknown — escalate
        return _escalate(db, conversation, body, reason="unknown_after_clarification")

    # First unknown — ask for clarification
    _update_state(db, conversation, ConversationState.AWAITING_CLARIFICATION)
    return get_template("unknown", language) or "Undskyld, kan du prøve igen?"


def _handle_clarification_attempt(
    db: Session,
    conversation: Conversation,
    body: str,
    context: str,
) -> str | None:
    """Handle a message received while awaiting clarification."""
    result = classify_message(body, context)
    _update_last_message_classification(db, conversation.id, result)

    if result.intent == "unknown":
        # Second failed attempt — escalate
        _update_state(db, conversation, ConversationState.IDLE)
        return _escalate(db, conversation, body, reason="unknown_after_clarification")

    # Successfully classified on retry
    _update_state(db, conversation, ConversationState.IDLE)
    return _route_by_intent(db, conversation, body, result, context)


def _confirmation_reply(db: Session, conversation: Conversation, language: str) -> str:
    """Build the reminder-confirmation reply.

    Appends a per-appointment-type "Husk at medbringe X i morgen." line when we
    can resolve the student's upcoming confirmed booking. The bring-list lives in
    config.yaml under ``appointment_types.<type>.what_to_bring`` and is Danish-only;
    for English, or when no upcoming booking / bring-list is found, fall back to
    the plain confirmation template.

    This is the deliberate home for "what to bring" (2026-06-03 decision): it lands
    when the student has just engaged (confirmed), not buried in the reminder they
    might skim past.
    """
    base = get_template("confirm_appointment", language)

    if language != "da":
        return base

    now_local = datetime.now(COPENHAGEN_TZ).replace(tzinfo=None)
    booking = (
        db.query(Booking)
        .filter(
            Booking.conversation_id == conversation.id,
            Booking.status == "confirmed",
            Booking.start_dt >= now_local - timedelta(hours=2),
        )
        .order_by(Booking.start_dt.asc())
        .first()
    )
    if booking is None:
        return base

    bring = (APPOINTMENT_TYPES.get(booking.appointment_type) or {}).get("what_to_bring")
    if not bring:
        return base

    return f"{base} Husk at medbringe {bring} i morgen."


def _handle_reminder_reply(
    db: Session,
    conversation: Conversation,
    body: str,
    result: ClassificationResult,
    context: str = "",
) -> str | None:
    """Handle a reply to a booking reminder."""
    _update_last_message_classification(db, conversation.id, result)

    # The reminder stashed which booking it was about; a NEJ / reschedule reply
    # should act on THAT booking, not re-ask the student which one (the reminder
    # named one specific slot).
    reminder_booking_id = None
    try:
        reminder_booking_id = (
            json.loads(conversation.context_json or "{}")
            .get("reminder", {})
            .get("booking_id")
        )
    except (ValueError, TypeError):
        pass

    if result.intent == "confirm_appointment":
        _update_state(db, conversation, ConversationState.IDLE)
        return _confirmation_reply(db, conversation, result.language)

    if result.intent in ("cancel_booking", "reschedule_booking"):
        _update_state(db, conversation, ConversationState.IDLE)
        response, new_state = start_cancellation_flow(
            db,
            conversation,
            body,
            result.language,
            result.intent,
            target_booking_id=reminder_booking_id,
        )
        _update_state(db, conversation, new_state)
        return response

    # Anything else during reminder flow — drop the AWAITING_CONFIRMATION lock
    # and re-route through the normal classifier. The reminder was informational;
    # the student can confirm later by texting JA. Escalating on every off-topic
    # message ("hvor mødes vi?", "hvad koster det?", "kan jeg booke en til?")
    # was hitting the owner over real-student questions Lisa can answer herself.
    _update_state(db, conversation, ConversationState.IDLE)
    return _route_by_intent(db, conversation, body, result, context)


def _escalate(
    db: Session,
    conversation: Conversation,
    body: str,
    reason: str,
    notify_student: bool = True,
    owner_prefix: str = "[LISA VIDERESTILLET]",
) -> str | None:
    """Escalate a message to the owner."""
    _update_state(db, conversation, ConversationState.IDLE)

    # Log escalation
    last_msg = (
        db.query(Message)
        .filter_by(conversation_id=conversation.id, direction="inbound")
        .order_by(Message.timestamp.desc())
        .first()
    )
    if last_msg:
        escalation = Escalation(message_id=last_msg.id, reason=reason)
        db.add(escalation)
        db.commit()

    # Forward to owner
    owner_msg = f"{owner_prefix} Fra {conversation.phone_number}: {body}"
    if OWNER_PHONE:
        send_sms(OWNER_PHONE, owner_msg)
    else:
        logger.warning("No OWNER_PHONE configured, cannot forward escalation")

    logger.info("Escalated: phone=%s reason=%s", conversation.phone_number, reason)

    if notify_student:
        # Detect language from recent messages
        language = _detect_language_from_history(db, conversation.id)
        return get_template("unknown_escalated", language)

    return None


def _update_state(db: Session, conversation: Conversation, new_state: ConversationState):
    """Update conversation state and commit.

    Always commits so handlers that mutate ``conversation.context_json``
    without changing state still have their changes persisted (e.g., the
    multi-booking disambiguation branch in cancellation.handler).
    """
    if conversation.state != new_state.value:
        conversation.state = new_state.value
        conversation.updated_at = datetime.now(timezone.utc)
    db.commit()


def _update_last_message_classification(
    db: Session, conversation_id: int, result: ClassificationResult
):
    """Update the most recent inbound message with classification data."""
    msg = (
        db.query(Message)
        .filter_by(conversation_id=conversation_id, direction="inbound")
        .order_by(Message.timestamp.desc())
        .first()
    )
    if msg:
        msg.intent = result.intent
        msg.confidence = result.confidence
        msg.language = result.language
        db.commit()


def _get_conversation_context(db: Session, conversation_id: int, limit: int = 10) -> str:
    """Get recent message history as context string."""
    messages = (
        db.query(Message)
        .filter_by(conversation_id=conversation_id)
        .order_by(Message.timestamp.desc())
        .limit(limit)
        .all()
    )
    if not messages:
        return ""

    lines = []
    for msg in reversed(messages):
        prefix = "Elev" if msg.direction == "inbound" else "Lisa"
        lines.append(f"{prefix}: {msg.body}")
    return "\n".join(lines)


def _detect_language_from_history(db: Session, conversation_id: int) -> str:
    """Detect language from the most recent classified message."""
    msg = (
        db.query(Message)
        .filter_by(conversation_id=conversation_id, direction="inbound")
        .filter(Message.language.isnot(None))
        .order_by(Message.timestamp.desc())
        .first()
    )
    return msg.language if msg and msg.language else "da"
