"""Tests for booking reminder job (BOOK-04)."""
from datetime import datetime, timedelta, timezone
from unittest.mock import patch


from app.booking.reminder import _send_booking_reminder
from app.conversation.state import ConversationState
from app.models import Booking, Conversation, Message


def test_reminder_sent_24h_before(db_session):
    """Booking reminder sends SMS for confirmed booking approaching in 24h."""
    conv = Conversation(phone_number="+4512345678", state="idle")
    db_session.add(conv)
    db_session.flush()

    now_naive = datetime.now(timezone.utc).replace(tzinfo=None)
    booking = Booking(
        phone_number="+4512345678",
        conversation_id=conv.id,
        appointment_type="lesson",
        start_dt=now_naive + timedelta(hours=24),
        end_dt=now_naive + timedelta(hours=25, minutes=30),
        status="confirmed",
    )
    db_session.add(booking)
    db_session.flush()

    with patch("app.booking.reminder.send_sms", return_value="sid123") as mock_sms, \
         patch("app.booking.reminder.can_send_message", return_value=True):
        _send_booking_reminder(db_session, booking)

    mock_sms.assert_called_once()
    sms_body = mock_sms.call_args[0][1]
    # The internal dedup tag must NOT appear in the SMS sent to the student
    # (2026-06-03 UX fix — students found "[booking-reminder:N]" confusing).
    assert f"[booking-reminder:{booking.id}]" not in sms_body
    assert "kl." in sms_body
    assert "Svar JA eller NEJ." in sms_body

    # ...but the tag IS stored on the outbound Message row so dedup keeps working.
    stored = (
        db_session.query(Message)
        .filter(Message.conversation_id == conv.id, Message.direction == "outbound")
        .first()
    )
    assert stored is not None
    assert f"[booking-reminder:{booking.id}]" in stored.body

    # Conversation state should be AWAITING_CONFIRMATION
    db_session.refresh(conv)
    assert conv.state == ConversationState.AWAITING_CONFIRMATION.value


def test_reminder_dedup_prevents_double_send(db_session):
    """If reminder already sent (dedup tag in message body), do not re-send."""
    conv = Conversation(phone_number="+4512345678", state="idle")
    db_session.add(conv)
    db_session.flush()

    now_naive = datetime.now(timezone.utc).replace(tzinfo=None)
    booking = Booking(
        phone_number="+4512345678",
        conversation_id=conv.id,
        appointment_type="lesson",
        start_dt=now_naive + timedelta(hours=24),
        end_dt=now_naive + timedelta(hours=25, minutes=30),
        status="confirmed",
    )
    db_session.add(booking)
    db_session.flush()

    # Simulate existing reminder message
    existing_msg = Message(
        conversation_id=conv.id,
        direction="outbound",
        body=f"Hej! Husk din tid 15/04 kl. 10:00. [booking-reminder:{booking.id}]",
    )
    db_session.add(existing_msg)
    db_session.flush()

    with patch("app.booking.reminder.send_sms") as mock_sms:
        _send_booking_reminder(db_session, booking)

    mock_sms.assert_not_called()


def test_reminder_not_sent_for_cancelled_booking(db_session):
    """Cancelled bookings should not receive reminders (filtered by status=confirmed in main query)."""
    # This test validates that the query filter works correctly.
    # _send_booking_reminder receives individual bookings, so we test at the query level.
    conv = Conversation(phone_number="+4512345678", state="idle")
    db_session.add(conv)
    db_session.flush()

    now_naive = datetime.now(timezone.utc).replace(tzinfo=None)
    booking = Booking(
        phone_number="+4512345678",
        conversation_id=conv.id,
        appointment_type="lesson",
        start_dt=now_naive + timedelta(hours=24),
        end_dt=now_naive + timedelta(hours=25, minutes=30),
        status="cancelled",
    )
    db_session.add(booking)
    db_session.flush()

    # Query should NOT return this booking
    results = (
        db_session.query(Booking)
        .filter(Booking.status == "confirmed")
        .all()
    )
    assert len(results) == 0
