"""24h reminder job for Lisa-created bookings from the Booking table.

Runs alongside the existing iCal-based reminder job (D-10: two reminder sources coexist).
Dedup uses booking ID tag in message body, matching the iCal reminder pattern.
"""
import json
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.config import REMINDER_HOURS_BEFORE
from app.conversation.state import ConversationState
from app.database import SessionLocal
from app.models import Booking, Conversation, Message
from app.sms.rate_limiter import can_send_message
from app.sms.sender import send_sms

logger = logging.getLogger(__name__)

COPENHAGEN_TZ = ZoneInfo("Europe/Copenhagen")


def check_and_send_booking_reminders():
    """Poll Booking table and send 24h reminders for confirmed bookings.

    Runs every 15 minutes via APScheduler. Queries bookings with start_dt
    in the 23-25h window from now. Uses dedup tag to prevent double-sends.
    """
    db = SessionLocal()
    try:
        # Fix #4: use Copenhagen local time to match how Booking.start_dt is stored
        # (naive Copenhagen local). Using naive UTC here caused reminders to fire
        # ~2 hours early in summer (CEST = UTC+2).
        now = datetime.now(COPENHAGEN_TZ).replace(tzinfo=None)
        window_start = now + timedelta(hours=REMINDER_HOURS_BEFORE - 1)
        window_end = now + timedelta(hours=REMINDER_HOURS_BEFORE + 1)

        upcoming = (
            db.query(Booking)
            .filter(
                Booking.start_dt >= window_start,
                Booking.start_dt <= window_end,
                Booking.status == "confirmed",
            )
            .all()
        )

        if not upcoming:
            logger.debug("No upcoming bookings in reminder window")
            return

        for booking in upcoming:
            _send_booking_reminder(db, booking)
    finally:
        db.close()


def _send_booking_reminder(db: Session, booking: Booking):
    """Send a reminder SMS for a single booking, with dedup.

    Dedup tag format: [booking-reminder:{booking.id}]
    """
    phone = booking.phone_number
    dedup_tag = f"[booking-reminder:{booking.id}]"

    # Get or create conversation
    conversation = db.query(Conversation).filter_by(phone_number=phone).first()
    if not conversation:
        conversation = Conversation(phone_number=phone)
        db.add(conversation)
        db.flush()

    # Dedup check — search for existing outbound message with this tag
    existing = (
        db.query(Message)
        .filter(
            Message.conversation_id == conversation.id,
            Message.direction == "outbound",
            Message.body.contains(dedup_tag),
        )
        .first()
    )
    if existing:
        logger.debug("Booking reminder already sent for booking %d to %s", booking.id, phone)
        return

    # Rate limit check
    if not can_send_message(db, conversation.id):
        logger.warning("Rate limited, skipping booking reminder for %s", phone)
        return

    # Format time in Copenhagen local time
    # Booking start_dt is naive Copenhagen local (from slot engine)
    local_dt = booking.start_dt.replace(tzinfo=COPENHAGEN_TZ)
    local_time = local_dt.strftime("%d/%m kl. %H:%M")

    # Clean text for the student — the internal dedup tag is deliberately NOT in
    # the SMS (students saw "[booking-reminder:N]" and found it confusing).
    # It is re-appended to the stored Message body below so dedup still works.
    reminder_text = (
        f"Hej! Husk din tid {local_time}. "
        f"Kan du bekræfte at du kommer? Svar JA eller NEJ."
    )

    # Send SMS
    sid = send_sms(phone, reminder_text)
    if not sid:
        logger.error("Failed to send booking reminder to %s", phone)
        return

    # Log outbound message — store WITH the dedup tag appended (invisible to the
    # student) so the dedup query keeps matching on subsequent reminder polls.
    msg = Message(
        conversation_id=conversation.id,
        direction="outbound",
        body=f"{reminder_text} {dedup_tag}",
    )
    db.add(msg)

    # Set conversation state to awaiting_confirmation, but only if the
    # student is not in an active flow (booking, lead intake, clarification).
    # Overwriting mid-flow state would break their in-progress conversation.
    _ACTIVE_FLOW_STATES = {
        ConversationState.BOOKING_SELECT_TYPE.value,
        ConversationState.BOOKING_OFFER_SLOT.value,
        ConversationState.BOOKING_SELECT_DAY.value,
        ConversationState.BOOKING_SELECT_TIME.value,
        ConversationState.LEAD_INTAKE_NAME.value,
        ConversationState.LEAD_INTAKE_LICENSE.value,
        ConversationState.LEAD_INTAKE_DATE.value,
        ConversationState.AWAITING_CLARIFICATION.value,
    }
    if conversation.state in _ACTIVE_FLOW_STATES:
        logger.info(
            "Skipping reminder state change for %s (active flow: %s)",
            phone,
            conversation.state,
        )
    else:
        conversation.state = ConversationState.AWAITING_CONFIRMATION.value
        # Remember which booking this reminder is about so a "NEJ" / reschedule
        # reply can act on THIS specific booking instead of re-asking the student
        # to disambiguate — the reminder already named one slot. Only stored when
        # we actually take the AWAITING_CONFIRMATION lock (not mid active flow).
        conversation.context_json = json.dumps(
            {"reminder": {"booking_id": booking.id}}, ensure_ascii=False
        )
    db.commit()

    logger.info("Sent booking reminder to %s for booking %d at %s", phone, booking.id, local_time)
