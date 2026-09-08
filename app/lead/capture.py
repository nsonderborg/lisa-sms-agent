import json
import logging

from sqlalchemy.orm import Session

from app.config import LICENSE_TYPES, OWNER_PHONE
from app.conversation.state import ConversationState
from app.models import Conversation, Lead
from app.sms.sender import send_sms

logger = logging.getLogger(__name__)

# License type display for SMS
_license_options = ", ".join(LICENSE_TYPES) if LICENSE_TYPES else "B"


def get_student_name(db: Session, phone_number: str) -> str | None:
    """Return the most recent stored name for this phone number, or None.

    Lead.phone_number is not unique — a single student may have multiple Lead
    rows over time. We pick the most recent row with a usable name.
    """
    lead = (
        db.query(Lead)
        .filter(Lead.phone_number == phone_number)
        .order_by(Lead.created_at.desc())
        .first()
    )
    if lead and lead.name:
        name = lead.name.strip()
        if name and name != "Ukendt":
            return name
    return None


def upsert_student_name(db: Session, phone_number: str, name: str) -> None:
    """Persist a student's name. Updates the most recent Lead row if one
    exists, otherwise inserts a new minimal Lead row.
    """
    lead = (
        db.query(Lead)
        .filter(Lead.phone_number == phone_number)
        .order_by(Lead.created_at.desc())
        .first()
    )
    if lead is None:
        lead = Lead(phone_number=phone_number, name=name, status="new")
        db.add(lead)
    else:
        lead.name = name
    db.flush()


def get_student_lesson(db: Session, phone_number: str) -> int | None:
    """Return the most recent stored lektionsplan number for this phone, or None.

    999.37 pilot seed. Mirrors get_student_name's most-recent-row semantics.
    """
    lead = (
        db.query(Lead)
        .filter(Lead.phone_number == phone_number)
        .order_by(Lead.created_at.desc())
        .first()
    )
    if lead is not None:
        return lead.current_lesson
    return None


def set_student_lesson(db: Session, phone_number: str, lesson: int | None) -> None:
    """Persist a student's current lektionsplan number on the most recent Lead row.

    999.37 pilot seed. The name pre-flight runs first in the booking flow, so a
    Lead row normally already exists; a minimal row is inserted if not.
    """
    lead = (
        db.query(Lead)
        .filter(Lead.phone_number == phone_number)
        .order_by(Lead.created_at.desc())
        .first()
    )
    if lead is None:
        lead = Lead(phone_number=phone_number, current_lesson=lesson, status="new")
        db.add(lead)
    else:
        lead.current_lesson = lesson
    db.flush()


def handle_lead_step(
    db: Session,
    conversation: Conversation,
    body: str,
    language: str,
) -> tuple[str, ConversationState]:
    """Handle one step of the lead capture flow. Returns (response_text, new_state)."""
    state = ConversationState(conversation.state)
    context = _get_context(conversation)

    if state == ConversationState.LEAD_INTAKE_NAME:
        return _handle_name(db, conversation, body, language, context)

    if state == ConversationState.LEAD_INTAKE_LICENSE:
        return _handle_license(db, conversation, body, language, context)

    if state == ConversationState.LEAD_INTAKE_DATE:
        return _handle_date(db, conversation, body, language, context)

    # Shouldn't happen, but reset gracefully
    return _fallback(language), ConversationState.IDLE


def _handle_name(
    db: Session,
    conversation: Conversation,
    body: str,
    language: str,
    context: dict,
) -> tuple[str, ConversationState]:
    """Capture the lead's name."""
    name = body.strip()
    if len(name) < 2:
        if language == "en":
            return "Could you please tell me your full name?", ConversationState.LEAD_INTAKE_NAME
        return "Hvad er dit fulde navn?", ConversationState.LEAD_INTAKE_NAME

    context["name"] = name
    _save_context(conversation, context)

    if language == "en":
        return f"Thanks {name}! Which license type? ({_license_options})", ConversationState.LEAD_INTAKE_LICENSE
    return f"Tak {name}! Hvilken type kørekort? ({_license_options})", ConversationState.LEAD_INTAKE_LICENSE


def _handle_license(
    db: Session,
    conversation: Conversation,
    body: str,
    language: str,
    context: dict,
) -> tuple[str, ConversationState]:
    """Capture the desired license type."""
    license_type = body.strip().upper()

    # Accept if it matches or is close enough
    if license_type not in LICENSE_TYPES:
        # Be lenient — accept it anyway and let owner sort it out
        pass

    context["license_type"] = license_type
    _save_context(conversation, context)

    if language == "en":
        return "When would you like to start? (e.g., 'April' or 'as soon as possible')", ConversationState.LEAD_INTAKE_DATE
    return "Hvornår vil du gerne starte? (f.eks. 'april' eller 'hurtigst muligt')", ConversationState.LEAD_INTAKE_DATE


def _handle_date(
    db: Session,
    conversation: Conversation,
    body: str,
    language: str,
    context: dict,
) -> tuple[str, ConversationState]:
    """Capture desired start date and finalize the lead."""
    desired_start = body.strip()
    context["desired_start"] = desired_start

    name = context.get("name", "Ukendt")
    license_type = context.get("license_type", "?")

    # Save lead to database
    lead = Lead(
        phone_number=conversation.phone_number,
        name=name,
        license_type=license_type,
        desired_start=desired_start,
        status="new",
    )
    db.add(lead)

    # Clear context
    _save_context(conversation, {})
    db.commit()

    # Send summary to owner
    if OWNER_PHONE:
        summary = (
            f"[LISA NY LEAD]\n"
            f"Navn: {name}\n"
            f"Tlf: {conversation.phone_number}\n"
            f"Kørekort: {license_type}\n"
            f"Ønsket start: {desired_start}"
        )
        send_sms(OWNER_PHONE, summary)

    logger.info("Lead captured: %s (%s) - %s - %s", name, conversation.phone_number, license_type, desired_start)

    if language == "en":
        return f"Thanks {name}! We've noted your interest and will be in touch soon 😊", ConversationState.IDLE
    return f"Tak {name}! Vi har noteret din interesse og vender tilbage snarest 😊", ConversationState.IDLE


def _fallback(language: str) -> str:
    if language == "en":
        return "Thanks for your message! We'll get back to you shortly."
    return "Tak for din besked! Vi vender tilbage snarest."


def _get_context(conversation: Conversation) -> dict:
    try:
        return json.loads(conversation.context_json or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}


def _save_context(conversation: Conversation, context: dict):
    conversation.context_json = json.dumps(context, ensure_ascii=False)
