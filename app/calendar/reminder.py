import json
import logging
from datetime import datetime, timezone, timedelta

from sqlalchemy.orm import Session

from app.calendar.ical_poller import CalendarEvent, fetch_events
from app.config import NO_REPLY_TIMEOUT_HOURS, OWNER_PHONE, REMINDER_HOURS_BEFORE
from app.conversation.state import ConversationState
from app.database import SessionLocal
from app.models import Booking, Conversation, Escalation, Message
from app.sms.rate_limiter import can_send_message
from app.sms.sender import send_sms

logger = logging.getLogger(__name__)


def check_and_send_reminders():
    """Poll iCal feed and send reminders for appointments ~24h out."""
    events = fetch_events()
    if not events:
        return

    now = datetime.now(timezone.utc)
    # Window: 23-25 hours from now (to handle polling jitter)
    window_start = now + timedelta(hours=REMINDER_HOURS_BEFORE - 1)
    window_end = now + timedelta(hours=REMINDER_HOURS_BEFORE + 1)

    upcoming = [e for e in events if window_start <= e.start <= window_end and e.phone_number]

    if not upcoming:
        logger.debug("No upcoming events in reminder window")
        return

    db = SessionLocal()
    try:
        for event in upcoming:
            _send_reminder_for_event(db, event)
    finally:
        db.close()


def _send_reminder_for_event(db: Session, event: CalendarEvent):
    """Send a reminder SMS for a single calendar event, if not already sent."""
    phone = event.phone_number

    # Get or create conversation
    conversation = db.query(Conversation).filter_by(phone_number=phone).first()
    if not conversation:
        conversation = Conversation(phone_number=phone)
        db.add(conversation)
        db.flush()

    # Check if we already sent a reminder for this event (dedup by UID in message body)
    dedup_tag = f"[reminder:{event.uid}]"
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
        logger.debug("Reminder already sent for event %s to %s", event.uid, phone)
        return

    # Check rate limit
    if not can_send_message(db, conversation.id):
        logger.warning("Rate limited, skipping reminder for %s", phone)
        return

    # Format time for SMS
    local_time = event.start.strftime("%d/%m kl. %H:%M")
    # Clean text for the student — internal dedup tag NOT in the SMS (it confused
    # students). Re-appended to the stored Message body below so dedup still works.
    reminder_text = (
        f"Hej! Husk din køretime {local_time}. "
        f"Kan du bekræfte at du kommer? Svar JA eller NEJ."
    )

    # Send SMS
    sid = send_sms(phone, reminder_text)
    if not sid:
        logger.error("Failed to send reminder to %s", phone)
        return

    # Log outbound message — store WITH the dedup tag appended (invisible to the
    # student) so the dedup query keeps matching on subsequent reminder polls.
    msg = Message(
        conversation_id=conversation.id,
        direction="outbound",
        body=f"{reminder_text} {dedup_tag}",
    )
    db.add(msg)

    # Set conversation state to awaiting_confirmation
    conversation.state = ConversationState.AWAITING_CONFIRMATION.value
    db.commit()

    logger.info("Sent reminder to %s for event at %s", phone, event.start)


def check_no_reply_timeouts():
    """Check for reminders that haven't been replied to within the timeout window."""
    db = SessionLocal()
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=NO_REPLY_TIMEOUT_HOURS)

        # Find conversations stuck in awaiting_confirmation that were updated before the cutoff
        stale = (
            db.query(Conversation)
            .filter(
                Conversation.state == ConversationState.AWAITING_CONFIRMATION.value,
                Conversation.updated_at <= cutoff,
            )
            .all()
        )

        for conversation in stale:
            _handle_no_reply(db, conversation)

        # Reschedule nudge: at 1 hour of inactivity send a single prompt before the
        # 4-hour hard reset, so students who paused mid-reschedule can continue.
        nudge_cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
        reschedule_nudge_candidates = (
            db.query(Conversation)
            .filter(
                Conversation.state.in_([
                    ConversationState.RESCHEDULE_SELECT_SLOT.value,
                    ConversationState.RESCHEDULE_CONFIRM.value,
                ]),
                Conversation.updated_at <= nudge_cutoff,
                Conversation.updated_at > cutoff,  # not yet at the 4-hour hard-reset threshold
            )
            .all()
        )
        for conv in reschedule_nudge_candidates:
            _send_reschedule_nudge(db, conv)

        # Fix #6: also reset mid-flow conversations that have gone stale — the
        # existing timeout only covered AWAITING_CONFIRMATION, leaving students
        # permanently stuck in booking/cancel/lead states if they stopped replying.
        _RESCHEDULE_STATES = {
            ConversationState.RESCHEDULE_SELECT_SLOT.value,
            ConversationState.RESCHEDULE_CONFIRM.value,
        }
        _MID_FLOW_STATES = [
            ConversationState.BOOKING_SELECT_TYPE.value,
            ConversationState.BOOKING_OFFER_SLOT.value,
            ConversationState.BOOKING_SELECT_DAY.value,
            ConversationState.BOOKING_SELECT_TIME.value,
            ConversationState.CANCEL_CONFIRM.value,
            ConversationState.RESCHEDULE_SELECT_SLOT.value,
            ConversationState.RESCHEDULE_CONFIRM.value,
            ConversationState.AWAITING_CLARIFICATION.value,
            ConversationState.LEAD_INTAKE_NAME.value,
            ConversationState.LEAD_INTAKE_LICENSE.value,
            ConversationState.LEAD_INTAKE_DATE.value,
        ]
        stale_flows = (
            db.query(Conversation)
            .filter(
                Conversation.state.in_(_MID_FLOW_STATES),
                Conversation.updated_at <= cutoff,
            )
            .all()
        )
        for conv in stale_flows:
            logger.info("Resetting stale mid-flow conversation for %s (state: %s)", conv.phone_number, conv.state)
            # Revert any reschedule_pending booking so the student's original slot is restored.
            if conv.state in _RESCHEDULE_STATES:
                try:
                    ctx = json.loads(conv.context_json or "{}")
                    booking_id = ctx.get("cancellation", {}).get("booking_id")
                    if booking_id:
                        booking = (
                            db.query(Booking)
                            .filter(
                                Booking.id == booking_id,
                                Booking.phone_number == conv.phone_number,
                                Booking.status == "reschedule_pending",
                            )
                            .first()
                        )
                        if booking:
                            booking.status = "confirmed"
                            logger.info("Reverted reschedule_pending booking %d for %s", booking_id, conv.phone_number)
                except Exception:
                    logger.exception("Failed to revert reschedule_pending for %s", conv.phone_number)
            conv.state = ConversationState.IDLE.value
            conv.context_json = "{}"
        if stale_flows:
            db.commit()
    finally:
        db.close()


def _send_reschedule_nudge(db: Session, conversation: Conversation) -> None:
    """Send a one-time nudge to a student who paused mid-reschedule.

    Sets reschedule_nudge_sent in context_json so it only fires once.
    The student's reply ('ja'/'nej') is handled naturally by the existing
    reschedule state machine — no special reply handling needed here.
    """
    try:
        ctx = json.loads(conversation.context_json or "{}")
    except (json.JSONDecodeError, TypeError):
        ctx = {}

    if ctx.get("reschedule_nudge_sent"):
        return

    if not can_send_message(db, conversation.id):
        logger.warning("Rate limited, skipping reschedule nudge for %s", conversation.phone_number)
        return

    nudge_text = (
        "Du var ved at omplanlægge din tid. Vil du fortsætte? "
        "Svar JA for at genoptage eller NEJ for at beholde din nuværende tid."
    )
    sid = send_sms(conversation.phone_number, nudge_text)
    if not sid:
        logger.error("Failed to send reschedule nudge to %s", conversation.phone_number)
        return

    db.add(Message(conversation_id=conversation.id, direction="outbound", body=nudge_text))
    ctx["reschedule_nudge_sent"] = True
    conversation.context_json = json.dumps(ctx, ensure_ascii=False)
    db.commit()
    logger.info("Sent reschedule nudge to %s", conversation.phone_number)


def _handle_no_reply(db: Session, conversation: Conversation):
    """Handle a student who didn't reply to a reminder."""
    logger.info("No reply timeout for %s", conversation.phone_number)

    # Reset state
    conversation.state = ConversationState.IDLE.value
    db.flush()

    # Log escalation
    last_outbound = (
        db.query(Message)
        .filter_by(conversation_id=conversation.id, direction="outbound")
        .order_by(Message.timestamp.desc())
        .first()
    )
    if last_outbound:
        escalation = Escalation(
            message_id=last_outbound.id,
            reason="no_reply_to_reminder",
        )
        db.add(escalation)

    # Alert owner
    if OWNER_PHONE:
        alert = f"[LISA] Ingen svar fra {conversation.phone_number} på påmindelse. Tjek venligst."
        send_sms(OWNER_PHONE, alert)

    db.commit()
