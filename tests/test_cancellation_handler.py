"""Tests for cancellation and rescheduling flow handler (CANC-01 through CANC-04)."""
import json
from datetime import datetime, timedelta
from unittest.mock import patch


from app.cancellation.handler import (
    handle_cancellation_step,
    revert_reschedule_pending,
    start_cancellation_flow,
)
from app.conversation.state import ConversationState
from app.models import Booking, Conversation


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_conversation(db, phone="+4512345678", state="idle"):
    conv = Conversation(phone_number=phone, state=state, context_json="{}")
    db.add(conv)
    db.flush()
    return conv


def _create_test_booking(db, conversation, appt_type="lesson", days_ahead=7, status="confirmed"):
    """Create a Booking for testing with start_dt days_ahead from now."""
    start = datetime.now() + timedelta(days=days_ahead)
    end = start + timedelta(minutes=90)
    booking = Booking(
        phone_number=conversation.phone_number,
        conversation_id=conversation.id,
        appointment_type=appt_type,
        start_dt=start,
        end_dt=end,
        gcal_event_id="test_gcal_id_123",
        status=status,
    )
    db.add(booking)
    db.commit()
    return booking


# ---------------------------------------------------------------------------
# D-01: no confirmed bookings
# ---------------------------------------------------------------------------

@patch("app.cancellation.handler.send_sms")
def test_cancel_no_bookings(mock_sms, db_session):
    """D-01: No upcoming confirmed bookings -> IDLE with informative message."""
    conv = _make_conversation(db_session)
    response, new_state = start_cancellation_flow(
        db_session, conv, "aflys", "da", "cancel_booking"
    )
    assert new_state == ConversationState.IDLE
    assert "ingen" in response.lower() or "no upcoming" in response.lower()


@patch("app.cancellation.handler.send_sms")
def test_cancel_no_bookings_english(mock_sms, db_session):
    """D-01: No bookings in English language."""
    conv = _make_conversation(db_session)
    response, new_state = start_cancellation_flow(
        db_session, conv, "cancel", "en", "cancel_booking"
    )
    assert new_state == ConversationState.IDLE
    assert "no upcoming" in response.lower()


# ---------------------------------------------------------------------------
# D-01: single confirmed booking — goes straight to confirmation
# ---------------------------------------------------------------------------

@patch("app.cancellation.handler.send_sms")
def test_cancel_single_booking_direct(mock_sms, db_session):
    """D-01: Exactly one confirmed booking -> directly enters CANCEL_CONFIRM with details."""
    conv = _make_conversation(db_session)
    _create_test_booking(db_session, conv)

    response, new_state = start_cancellation_flow(
        db_session, conv, "aflys", "da", "cancel_booking"
    )
    assert new_state == ConversationState.CANCEL_CONFIRM
    # Should mention the booking type and time
    assert "kl." in response
    assert "aflys" in response.lower() or "aflyse" in response.lower() or "ja" in response.lower()


# ---------------------------------------------------------------------------
# D-01: multiple bookings — disambiguation list
# ---------------------------------------------------------------------------

@patch("app.cancellation.handler.send_sms")
def test_cancel_multiple_bookings_disambiguation(mock_sms, db_session):
    """D-01: Multiple confirmed bookings -> numbered list shown, state CANCEL_CONFIRM."""
    conv = _make_conversation(db_session)
    _create_test_booking(db_session, conv, appt_type="lesson", days_ahead=5)
    _create_test_booking(db_session, conv, appt_type="test", days_ahead=10)

    response, new_state = start_cancellation_flow(
        db_session, conv, "aflys", "da", "cancel_booking"
    )
    assert new_state == ConversationState.CANCEL_CONFIRM
    # Numbered list expected
    assert "1." in response
    assert "2." in response


# ---------------------------------------------------------------------------
# Reminder fast-path: target_booking_id acts on the named booking directly
# ---------------------------------------------------------------------------

@patch("app.cancellation.handler.send_sms")
def test_cancel_target_booking_skips_disambiguation(mock_sms, db_session):
    """A NEJ reply to a day-before reminder should cancel THAT booking directly,
    not show the numbered disambiguation list, even with multiple bookings."""
    conv = _make_conversation(db_session)
    _create_test_booking(db_session, conv, appt_type="lesson", days_ahead=5)
    target = _create_test_booking(db_session, conv, appt_type="test", days_ahead=10)

    response, new_state = start_cancellation_flow(
        db_session, conv, "nej", "da", "cancel_booking", target_booking_id=target.id
    )

    assert new_state == ConversationState.CANCEL_CONFIRM
    # Straight to confirming the one booking — no "1." / "2." disambiguation list
    assert "1." not in response and "2." not in response
    ctx = json.loads(conv.context_json)
    assert ctx["cancellation"]["booking_id"] == target.id


@patch("app.cancellation.handler.extract_preferences")
@patch("app.cancellation.handler.send_sms")
def test_cancel_narrows_by_stated_time(mock_sms, mock_extract, db_session):
    """'aflys min tid kl 14 i morgen' should resolve to the 14:00 booking
    directly, not show the disambiguation list, when the words pick exactly one."""
    from app.booking.preference_extractor import BookingPreferences

    conv = _make_conversation(db_session)
    day = (datetime.now() + timedelta(days=1)).date()
    b14 = Booking(
        phone_number=conv.phone_number,
        conversation_id=conv.id,
        appointment_type="lesson",
        start_dt=datetime(day.year, day.month, day.day, 14, 0),
        end_dt=datetime(day.year, day.month, day.day, 15, 30),
        status="confirmed",
    )
    b1730 = Booking(
        phone_number=conv.phone_number,
        conversation_id=conv.id,
        appointment_type="lesson",
        start_dt=datetime(day.year, day.month, day.day, 17, 30),
        end_dt=datetime(day.year, day.month, day.day, 19, 0),
        status="confirmed",
    )
    db_session.add_all([b14, b1730])
    db_session.commit()

    mock_extract.return_value = BookingPreferences(
        appointment_type=None,
        days=[],
        time_of_day=None,
        preferred_time="14:00",
        target_date=day.isoformat(),
    )

    response, new_state = start_cancellation_flow(
        db_session, conv, "aflys min tid kl 14 i morgen", "da", "cancel_booking"
    )

    assert new_state == ConversationState.CANCEL_CONFIRM
    assert "1." not in response and "2." not in response
    ctx = json.loads(conv.context_json)
    assert ctx["cancellation"]["booking_id"] == b14.id


@patch("app.cancellation.handler.send_sms")
def test_cancel_target_booking_falls_through_when_gone(mock_sms, db_session):
    """If the targeted booking no longer exists (already cancelled / stale id),
    fall through to the normal lookup rather than erroring."""
    conv = _make_conversation(db_session)
    remaining = _create_test_booking(db_session, conv, appt_type="lesson", days_ahead=5)

    response, new_state = start_cancellation_flow(
        db_session, conv, "nej", "da", "cancel_booking", target_booking_id=999999
    )

    # Falls through; one remaining confirmed booking -> direct confirm on it
    assert new_state == ConversationState.CANCEL_CONFIRM
    ctx = json.loads(conv.context_json)
    assert ctx["cancellation"]["booking_id"] == remaining.id


# ---------------------------------------------------------------------------
# CANC-01: cancel within policy -> confirmation, no refund warning
# ---------------------------------------------------------------------------

@patch("app.cancellation.handler.build_calendar_service")
@patch("app.cancellation.handler.delete_event")
@patch("app.cancellation.handler.send_sms")
@patch("app.cancellation.handler.OWNER_PHONE", "+4500000000")
@patch("app.cancellation.handler.GCAL_CALENDAR_ID", "")
@patch("app.cancellation.handler.is_within_cancellation_policy", return_value=True)
def test_cancel_within_policy(
    mock_policy, mock_sms, mock_delete, mock_build_service, db_session
):
    """CANC-01: Cancel within policy -> booking cancelled, no refund warning, calendar deleted."""
    conv = _make_conversation(db_session)
    booking = _create_test_booking(db_session, conv, days_ahead=7)

    # Set up CANCEL_CONFIRM state with booking_id in context
    conv.state = ConversationState.CANCEL_CONFIRM
    conv.context_json = json.dumps({
        "cancellation": {
            "intent": "cancel_booking",
            "booking_id": booking.id,
            "appointment_type": "lesson",
        }
    })
    db_session.commit()

    response, new_state = handle_cancellation_step(db_session, conv, "ja", "da")

    assert new_state == ConversationState.IDLE
    db_session.refresh(booking)
    assert booking.status == "cancelled"
    assert booking.cancelled_at is not None
    assert "refusion" not in response.lower()
    mock_delete.assert_called_once_with(mock_build_service.return_value, "", "test_gcal_id_123")


# ---------------------------------------------------------------------------
# CANC-02: late cancel -> warning about no-refund
# ---------------------------------------------------------------------------

@patch("app.cancellation.handler.build_calendar_service")
@patch("app.cancellation.handler.delete_event")
@patch("app.cancellation.handler.send_sms")
@patch("app.cancellation.handler.OWNER_PHONE", "+4500000000")
@patch("app.cancellation.handler.is_within_cancellation_policy", return_value=False)
def test_cancel_late_warning(
    mock_policy, mock_sms, mock_delete, mock_build_service, db_session
):
    """CANC-02: Late cancel -> booking cancelled but refund warning included."""
    conv = _make_conversation(db_session)
    booking = _create_test_booking(db_session, conv, days_ahead=1)

    conv.state = ConversationState.CANCEL_CONFIRM
    conv.context_json = json.dumps({
        "cancellation": {
            "intent": "cancel_booking",
            "booking_id": booking.id,
            "appointment_type": "lesson",
        }
    })
    db_session.commit()

    response, new_state = handle_cancellation_step(db_session, conv, "ja", "da")

    assert new_state == ConversationState.IDLE
    db_session.refresh(booking)
    assert booking.status == "cancelled"
    # The confirmation is delivered via the Twilio API (send_sms), not the TwiML
    # reply, so the handler returns silent and the refund warning is in the send.
    assert response == ""
    sent_bodies = [c.args[1] for c in mock_sms.call_args_list]
    assert any("refusion" in b.lower() or "frist" in b.lower() for b in sent_bodies)


# ---------------------------------------------------------------------------
# Cancel rejection — booking kept
# ---------------------------------------------------------------------------

@patch("app.cancellation.handler.send_sms")
def test_cancel_rejection(mock_sms, db_session):
    """Saying 'nej' during CANCEL_CONFIRM -> booking stays confirmed, state IDLE."""
    conv = _make_conversation(db_session)
    booking = _create_test_booking(db_session, conv)

    conv.state = ConversationState.CANCEL_CONFIRM
    conv.context_json = json.dumps({
        "cancellation": {
            "intent": "cancel_booking",
            "booking_id": booking.id,
            "appointment_type": "lesson",
        }
    })
    db_session.commit()

    response, new_state = handle_cancellation_step(db_session, conv, "nej", "da")

    assert new_state == ConversationState.IDLE
    db_session.refresh(booking)
    assert booking.status == "confirmed"


# ---------------------------------------------------------------------------
# D-09: reschedule sets booking to reschedule_pending
# ---------------------------------------------------------------------------

@patch("app.cancellation.handler.find_earliest_slot", return_value=datetime(2026, 4, 25, 10, 0))
@patch("app.cancellation.handler.send_sms")
def test_reschedule_marks_pending(mock_sms, mock_find_slot, db_session):
    """D-09: Starting reschedule flow sets old booking.status to reschedule_pending."""
    conv = _make_conversation(db_session)
    booking = _create_test_booking(db_session, conv)

    response, new_state = start_cancellation_flow(
        db_session, conv, "omlæg", "da", "reschedule_booking"
    )

    db_session.refresh(booking)
    assert booking.status == "reschedule_pending"
    assert new_state == ConversationState.RESCHEDULE_SELECT_SLOT


# ---------------------------------------------------------------------------
# CANC-03 / D-10: reschedule atomic commit
# ---------------------------------------------------------------------------

@patch("app.cancellation.handler.build_calendar_service")
@patch("app.cancellation.handler.create_event", return_value={"id": "new_gcal_evt"})
@patch("app.cancellation.handler.delete_event")
@patch("app.cancellation.handler.send_sms")
@patch("app.cancellation.handler.OWNER_PHONE", "+4500000000")
def test_reschedule_atomic_commit(
    mock_sms, mock_delete, mock_create_event, mock_build_service, db_session
):
    """CANC-03, D-10: Atomic commit — old booking cancelled, new booking confirmed."""
    conv = _make_conversation(db_session)
    old_booking = _create_test_booking(db_session, conv, appt_type="lesson", days_ahead=7)
    old_booking.status = "reschedule_pending"
    db_session.commit()

    new_slot = datetime(2026, 4, 25, 10, 0)
    conv.state = ConversationState.RESCHEDULE_CONFIRM
    conv.context_json = json.dumps({
        "cancellation": {
            "intent": "reschedule_booking",
            "booking_id": old_booking.id,
            "appointment_type": "lesson",
            "offered_slot": new_slot.isoformat(),
        }
    })
    db_session.commit()

    response, new_state = handle_cancellation_step(db_session, conv, "ja", "da")

    assert new_state == ConversationState.IDLE

    # Old booking should be cancelled
    db_session.refresh(old_booking)
    assert old_booking.status == "cancelled"
    assert old_booking.cancelled_at is not None

    # New booking should exist and be confirmed
    new_booking = (
        db_session.query(Booking)
        .filter(
            Booking.phone_number == conv.phone_number,
            Booking.status == "confirmed",
            Booking.id != old_booking.id,
        )
        .first()
    )
    assert new_booking is not None
    assert new_booking.appointment_type == "lesson"
    assert new_booking.start_dt == new_slot

    # Calendar events
    mock_delete.assert_called_once()
    mock_create_event.assert_called_once()


# ---------------------------------------------------------------------------
# D-11: revert_reschedule_pending function
# ---------------------------------------------------------------------------

def test_revert_reschedule_pending_function(db_session):
    """D-11: revert_reschedule_pending sets booking status back to confirmed."""
    conv = _make_conversation(db_session)
    booking = _create_test_booking(db_session, conv, status="reschedule_pending")
    assert booking.status == "reschedule_pending"

    revert_reschedule_pending(db_session, booking.id, conv.phone_number)

    db_session.refresh(booking)
    assert booking.status == "confirmed"


def test_revert_reschedule_pending_wrong_phone(db_session):
    """D-11 security: revert does nothing when phone_number doesn't match."""
    conv = _make_conversation(db_session)
    booking = _create_test_booking(db_session, conv, status="reschedule_pending")

    revert_reschedule_pending(db_session, booking.id, "+4599999999")

    db_session.refresh(booking)
    # Status should not have changed
    assert booking.status == "reschedule_pending"


# ---------------------------------------------------------------------------
# D-11: abandoned reschedule reverts to confirmed
# ---------------------------------------------------------------------------

@patch("app.cancellation.handler.send_sms")
def test_reschedule_rejection_reverts(mock_sms, db_session):
    """D-11: Student says 'nej' during RESCHEDULE_SELECT_SLOT -> reverts to confirmed."""
    conv = _make_conversation(db_session)
    booking = _create_test_booking(db_session, conv, status="reschedule_pending")

    conv.state = ConversationState.RESCHEDULE_SELECT_SLOT
    conv.context_json = json.dumps({
        "cancellation": {
            "intent": "reschedule_booking",
            "booking_id": booking.id,
            "appointment_type": "lesson",
            "offered_slot": datetime(2026, 4, 25, 10, 0).isoformat(),
        }
    })
    db_session.commit()

    response, new_state = handle_cancellation_step(db_session, conv, "nej", "da")

    assert new_state == ConversationState.IDLE
    db_session.refresh(booking)
    assert booking.status == "confirmed"


# ---------------------------------------------------------------------------
# CANC-04: instructor notification after cancel
# ---------------------------------------------------------------------------

@patch("app.cancellation.handler.build_calendar_service")
@patch("app.cancellation.handler.delete_event")
@patch("app.cancellation.handler.send_sms")
@patch("app.cancellation.handler.OWNER_PHONE", "+4500000000")
@patch("app.cancellation.handler.is_within_cancellation_policy", return_value=True)
def test_instructor_notified_on_cancel(
    mock_policy, mock_sms, mock_delete, mock_build_service, db_session
):
    """CANC-04: After cancel, send_sms called with OWNER_PHONE and [LISA AFLYST] prefix."""
    conv = _make_conversation(db_session)
    booking = _create_test_booking(db_session, conv)

    conv.state = ConversationState.CANCEL_CONFIRM
    conv.context_json = json.dumps({
        "cancellation": {
            "intent": "cancel_booking",
            "booking_id": booking.id,
            "appointment_type": "lesson",
        }
    })
    db_session.commit()

    handle_cancellation_step(db_session, conv, "ja", "da")

    # Check that at least one SMS to owner with [LISA AFLYST]
    sms_calls = mock_sms.call_args_list
    owner_calls = [
        c for c in sms_calls
        if len(c[0]) >= 2 and c[0][0] == "+4500000000" and "[LISA AFLYST]" in c[0][1]
    ]
    assert len(owner_calls) >= 1


# ---------------------------------------------------------------------------
# CANC-04: instructor notification after reschedule
# ---------------------------------------------------------------------------

@patch("app.cancellation.handler.build_calendar_service")
@patch("app.cancellation.handler.create_event", return_value={"id": "new_gcal_evt"})
@patch("app.cancellation.handler.delete_event")
@patch("app.cancellation.handler.send_sms")
@patch("app.cancellation.handler.OWNER_PHONE", "+4500000000")
def test_instructor_notified_on_reschedule(
    mock_sms, mock_delete, mock_create_event, mock_build_service, db_session
):
    """CANC-04: After reschedule, send_sms called with OWNER_PHONE and [LISA AENDRET] prefix."""
    conv = _make_conversation(db_session)
    old_booking = _create_test_booking(db_session, conv, status="reschedule_pending")

    new_slot = datetime(2026, 4, 25, 10, 0)
    conv.state = ConversationState.RESCHEDULE_CONFIRM
    conv.context_json = json.dumps({
        "cancellation": {
            "intent": "reschedule_booking",
            "booking_id": old_booking.id,
            "appointment_type": "lesson",
            "offered_slot": new_slot.isoformat(),
        }
    })
    db_session.commit()

    handle_cancellation_step(db_session, conv, "ja", "da")

    sms_calls = mock_sms.call_args_list
    owner_calls = [
        c for c in sms_calls
        if len(c[0]) >= 2 and c[0][0] == "+4500000000" and "[LISA ÆNDRET]" in c[0][1]
    ]
    assert len(owner_calls) >= 1


# ---------------------------------------------------------------------------
# T-08-01: Phone number security filter
# ---------------------------------------------------------------------------

@patch("app.cancellation.handler.send_sms")
def test_phone_number_filter_security(mock_sms, db_session):
    """T-08-01: Booking for phone A is not visible when cancel requested from phone B."""
    conv_a = _make_conversation(db_session, phone="+4511111111")
    _create_test_booking(db_session, conv_a)

    # Phone B tries to cancel phone A's booking
    conv_b = _make_conversation(db_session, phone="+4522222222")
    response, new_state = start_cancellation_flow(
        db_session, conv_b, "aflys", "da", "cancel_booking"
    )

    assert new_state == ConversationState.IDLE
    assert "ingen" in response.lower() or "no upcoming" in response.lower()


# ---------------------------------------------------------------------------
# Edge: booking already past / in the past
# ---------------------------------------------------------------------------

@patch("app.cancellation.handler.send_sms")
def test_cancel_past_booking_not_visible(mock_sms, db_session):
    """D-01: Past bookings (start_dt in past) are not returned — student gets 'no bookings'."""
    conv = _make_conversation(db_session)
    # Create a booking in the past
    _create_test_booking(db_session, conv, days_ahead=-1)

    response, new_state = start_cancellation_flow(
        db_session, conv, "aflys", "da", "cancel_booking"
    )
    assert new_state == ConversationState.IDLE
    assert "ingen" in response.lower() or "no upcoming" in response.lower()


# ---------------------------------------------------------------------------
# Delete-guard refusals are distinguishable from transient backend failures
#
# Both paths swallow the exception so the student still gets their confirmation,
# but they mean opposite things operationally: a transient iCloud error retries
# on the next cancellation, whereas a guard refusal never will and needs a
# human. During the 2026-08 CalDAV outage the logs were full of the transient
# line, which is exactly when the two must not look alike.
# ---------------------------------------------------------------------------

@patch("app.cancellation.handler.build_calendar_service")
@patch("app.cancellation.handler.delete_event")
@patch("app.cancellation.handler.send_sms")
@patch("app.cancellation.handler.OWNER_PHONE", "+4500000000")
@patch("app.cancellation.handler.GCAL_CALENDAR_ID", "")
@patch("app.cancellation.handler.is_within_cancellation_policy", return_value=True)
def test_cancel_survives_safety_refusal_and_logs_it_distinctly(
    mock_policy, mock_sms, mock_delete, mock_build_service, db_session, caplog
):
    """A refused delete must not break the cancellation, and must log SAFETY —
    not the generic 'Failed to delete' line used for transient failures."""
    from app.calendar.client import CalendarSafetyError

    mock_delete.side_effect = CalendarSafetyError("outside the Lisa collection")

    conv = _make_conversation(db_session)
    booking = _create_test_booking(db_session, conv, days_ahead=7)
    conv.state = ConversationState.CANCEL_CONFIRM
    conv.context_json = json.dumps({
        "cancellation": {
            "intent": "cancel_booking",
            "booking_id": booking.id,
            "appointment_type": "lesson",
        }
    })
    db_session.commit()

    with caplog.at_level("ERROR"):
        response, new_state = handle_cancellation_step(db_session, conv, "ja", "da")

    # The student's cancellation still completes — the guard is not their problem.
    assert new_state == ConversationState.IDLE
    db_session.refresh(booking)
    assert booking.status == "cancelled"
    # Confirmation goes out via the API (handler returns "" so the webhook stays
    # silent and doesn't double-send), so assert on the SMS, not the return value.
    assert any(
        call.args and call.args[0] == conv.phone_number
        for call in mock_sms.call_args_list
    ), "student got no cancellation confirmation"

    assert "SAFETY" in caplog.text
    assert "must be removed by hand" in caplog.text
    # The generic transient-failure wording must NOT appear for a refusal.
    assert "Failed to delete calendar event" not in caplog.text


@patch("app.cancellation.handler.build_calendar_service")
@patch("app.cancellation.handler.delete_event")
@patch("app.cancellation.handler.send_sms")
@patch("app.cancellation.handler.OWNER_PHONE", "+4500000000")
@patch("app.cancellation.handler.GCAL_CALENDAR_ID", "")
@patch("app.cancellation.handler.is_within_cancellation_policy", return_value=True)
def test_cancel_transient_delete_failure_still_logs_generic_line(
    mock_policy, mock_sms, mock_delete, mock_build_service, db_session, caplog
):
    """The other direction: a real backend failure keeps the generic line, so the
    two causes stay tellable apart in the logs."""
    mock_delete.side_effect = RuntimeError("iCloud 401 Unauthorized")

    conv = _make_conversation(db_session)
    booking = _create_test_booking(db_session, conv, days_ahead=7)
    conv.state = ConversationState.CANCEL_CONFIRM
    conv.context_json = json.dumps({
        "cancellation": {
            "intent": "cancel_booking",
            "booking_id": booking.id,
            "appointment_type": "lesson",
        }
    })
    db_session.commit()

    with caplog.at_level("ERROR"):
        _, new_state = handle_cancellation_step(db_session, conv, "ja", "da")

    assert new_state == ConversationState.IDLE
    db_session.refresh(booking)
    assert booking.status == "cancelled"
    assert "Failed to delete calendar event" in caplog.text
    assert "SAFETY" not in caplog.text
