"""Shared owner-escalation helper.

Lets any flow handler (booking, cancellation) forward a stuck conversation to
the owner without importing ``app.conversation.manager`` (which imports the
handlers — a circular dependency). Mirrors ``manager._escalate`` so behaviour is
consistent: log an ``Escalation`` row, send the owner the ``[LISA VIDERESTILLET]``
SMS, and return the Danish/English student-facing "I'm forwarding you" message.
"""
import logging

from sqlalchemy.orm import Session

from app.config import OWNER_PHONE
from app.models import Conversation, Escalation, Message
from app.response.templates import get_template
from app.sms.sender import send_sms

logger = logging.getLogger(__name__)


def forward_to_owner(
    db: Session,
    conversation: Conversation,
    body: str,
    reason: str,
    language: str = "da",
) -> str:
    """Forward a stuck conversation to the owner and return the student reply.

    Used by flow dead-ends (e.g. a student who can't be understood after 3
    attempts) so a warm booking/cancellation lead is handed to a human instead
    of being silently dropped with a generic error.
    """
    last_msg = (
        db.query(Message)
        .filter_by(conversation_id=conversation.id, direction="inbound")
        .order_by(Message.timestamp.desc())
        .first()
    )
    if last_msg:
        db.add(Escalation(message_id=last_msg.id, reason=reason))
        db.commit()

    if OWNER_PHONE:
        send_sms(OWNER_PHONE, f"[LISA VIDERESTILLET] Fra {conversation.phone_number}: {body}")
    else:
        logger.warning("No OWNER_PHONE configured, cannot forward escalation")

    logger.info("Escalated: phone=%s reason=%s", conversation.phone_number, reason)

    return get_template("unknown_escalated", language) or (
        "Jeg sender din besked videre til kørelæreren, som vender tilbage hurtigst muligt."
    )
