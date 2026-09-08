"""Tests for the booking flow handler (BOOK-01, BOOK-03, BOOK-05)."""
import json
from datetime import datetime, timedelta
from unittest.mock import patch


from app.booking.handler import (
    handle_booking_step,
    start_booking_flow,
    _format_slot,
)
from app.conversation.state import ConversationState
from app.models import Booking, Conversation, Message


# -- One-booking-per-day cap (MAX_BOOKINGS_PER_DAY) --

def _mk_booking(db, conv, start_dt, status="confirmed", appt_type="lesson"):
    b = Booking(
        phone_number=conv.phone_number,
        conversation_id=conv.id,
        appointment_type=appt_type,
        start_dt=start_dt,
        end_dt=start_dt + timedelta(minutes=90),
        status=status,
    )
    db.add(b)
    db.flush()
    return b


def test_booking_count_on_date_counts_only_confirmed_same_day(db_session):
    from app.booking.handler import _booking_count_on_date

    conv = Conversation(phone_number="+4512345678", state="idle")
    db_session.add(conv)
    db_session.flush()

    day = (datetime.now() + timedelta(days=3)).date()
    other = (datetime.now() + timedelta(days=4)).date()
    _mk_booking(db_session, conv, datetime(day.year, day.month, day.day, 8, 0))
    _mk_booking(db_session, conv, datetime(day.year, day.month, day.day, 14, 0))
    _mk_booking(
        db_session, conv, datetime(day.year, day.month, day.day, 10, 0), status="cancelled"
    )
    _mk_booking(db_session, conv, datetime(other.year, other.month, other.day, 9, 0))

    assert _booking_count_on_date(db_session, conv.phone_number, day) == 2
    assert _booking_count_on_date(db_session, conv.phone_number, other) == 1


@patch("app.booking.handler.MAX_BOOKINGS_PER_DAY", 1)
@patch("app.booking.handler.send_sms")
def test_per_day_cap_blocks_second_same_day_booking(mock_sms, db_session):
    """With the cap at 1, confirming a second slot on a day the student already
    has a booking is rejected before any calendar write."""
    from app.booking.handler import _confirm_booking

    conv = Conversation(
        phone_number="+4512345678", state="booking_confirm", context_json="{}"
    )
    db_session.add(conv)
    db_session.flush()

    slot = (datetime.now() + timedelta(days=3)).replace(
        hour=14, minute=0, second=0, microsecond=0
    )
    _mk_booking(db_session, conv, slot.replace(hour=8, minute=0))  # existing same-day

    ctx = {"booking": {"offered_slot": slot.isoformat(), "appointment_type": "lesson"}}
    response, new_state = _confirm_booking(db_session, conv, "da", ctx)

    assert new_state == ConversationState.IDLE
    assert "pr. dag" in response.lower() or "anden dag" in response.lower()
    cnt = (
        db_session.query(Booking)
        .filter_by(phone_number=conv.phone_number, status="confirmed")
        .count()
    )
    assert cnt == 1


@patch("app.booking.handler.MAX_BOOKINGS_PER_DAY", 1)
@patch("app.booking.handler.extract_preferences")
def test_per_day_cap_denies_requested_booked_day_up_front(mock_extract, db_session):
    """Asking to book a day you're already booked is denied at flow entry, not
    after being offered slots (no bait-and-switch)."""
    from app.booking.preference_extractor import BookingPreferences

    conv = Conversation(phone_number="+4512345678", state="idle", context_json="{}")
    db_session.add(conv)
    db_session.flush()

    day = (datetime.now() + timedelta(days=2)).date()
    _mk_booking(db_session, conv, datetime(day.year, day.month, day.day, 8, 0))
    mock_extract.return_value = BookingPreferences(
        appointment_type="lesson",
        days=[],
        time_of_day=None,
        preferred_time=None,
        target_date=day.isoformat(),
    )

    response, new_state = start_booking_flow(db_session, conv, "book i morgen", "da")

    assert new_state == ConversationState.IDLE
    assert "pr. dag" in response.lower() or "anden dag" in response.lower()


@patch("app.booking.handler.MAX_BOOKINGS_PER_DAY", 1)
def test_offer_slot_on_date_blocks_already_booked_day_mid_flow(db_session):
    """Mid-flow steering to a day you're already booked is denied at the offer
    chokepoint, not just at confirm (the 'nej' then 'i morgen kl 13' case)."""
    from app.booking.handler import _offer_slot_on_date

    conv = Conversation(
        phone_number="+4512345678", state="booking_offer_slot", context_json="{}"
    )
    db_session.add(conv)
    db_session.flush()

    day = (datetime.now() + timedelta(days=2)).date()
    _mk_booking(db_session, conv, datetime(day.year, day.month, day.day, 17, 30))

    response, new_state = _offer_slot_on_date(
        db_session, conv, "da", {"booking": {}}, "lesson", day
    )

    assert new_state == ConversationState.IDLE
    assert "pr. dag" in response.lower() or "anden dag" in response.lower()


# -- BOOK-01: Booking flow entry and earliest slot offer --

@patch("app.booking.handler.OWNER_PHONE", "+4500000000")
@patch("app.booking.handler.send_sms")
@patch("app.booking.handler.find_earliest_slots")
@patch("app.booking.handler.extract_preferences")
def test_start_booking_with_type_offers_earliest_slot(
    mock_extract, mock_find_slots, mock_sms, db_session
):
    """When student says 'book koretime', type is extracted and earliest slot offered."""
    from app.booking.preference_extractor import BookingPreferences
    mock_extract.return_value = BookingPreferences(
        appointment_type="lesson", days=[], time_of_day=None, preferred_time=None
    )
    # Phase 999.34: the offer path leads with find_earliest_slots (plural).
    mock_find_slots.return_value = [datetime(2026, 4, 15, 8, 15)]  # naive UTC

    conv = Conversation(phone_number="+4512345678", state="idle")
    db_session.add(conv)
    db_session.flush()
    # Name on file skips the first-booking name pre-flight so we reach the offer.
    from app.lead.capture import upsert_student_name
    upsert_student_name(db_session, conv.phone_number, "Testelev")

    response, new_state = start_booking_flow(db_session, conv, "Jeg vil gerne booke en koretime", "da")

    assert new_state == ConversationState.BOOKING_OFFER_SLOT
    assert "kl." in response
    assert mock_find_slots.call_args[0][0] == "lesson"


@patch("app.booking.handler.extract_preferences")
def test_start_booking_without_type_asks_type(mock_extract, db_session):
    """When student says 'book' without type, Lisa asks for type (D-06)."""
    from app.booking.preference_extractor import BookingPreferences
    mock_extract.return_value = BookingPreferences(
        appointment_type=None, days=[], time_of_day=None, preferred_time=None
    )

    conv = Conversation(phone_number="+4512345678", state="idle")
    db_session.add(conv)
    db_session.flush()
    # Name on file skips the first-booking name pre-flight so we reach the type question.
    from app.lead.capture import upsert_student_name
    upsert_student_name(db_session, conv.phone_number, "Testelev")

    response, new_state = start_booking_flow(db_session, conv, "Jeg vil booke", "da")

    assert new_state == ConversationState.BOOKING_SELECT_TYPE
    assert "køretime" in response.lower() or "manøvrebane" in response.lower()


# -- Regression: band reply carrying a day must not drop the day --

@patch("app.booking.handler.find_slots_for_date")
@patch("app.booking.handler.extract_preferences")
def test_select_time_reply_with_day_keeps_day(mock_extract, mock_find_date, db_session):
    """'tirsdag eftermiddag' as a band-question answer must search Tuesday,
    not the soonest afternoon on ANY day.

    Regression for the live bug where _handle_select_time read only time_of_day
    and dropped target_date, offering Saturday for a Tuesday request.
    """
    from app.booking.preference_extractor import BookingPreferences
    mock_extract.return_value = BookingPreferences(
        appointment_type=None, days=["tirsdag"], time_of_day="eftermiddag",
        preferred_time=None, target_date="2026-06-02",
    )
    mock_find_date.return_value = [datetime(2026, 6, 2, 13, 0)]  # Tuesday afternoon

    conv = Conversation(
        phone_number="+4512345678",
        state=ConversationState.BOOKING_SELECT_TIME.value,
        context_json=json.dumps({"booking": {"appointment_type": "lesson"}}),
    )
    db_session.add(conv)
    db_session.flush()

    response, new_state = handle_booking_step(db_session, conv, "tirsdag eftermiddag", "da")

    # The day survived: slot search ran against the resolved Tuesday date + band.
    assert mock_find_date.called
    call = mock_find_date.call_args
    assert call.args[1].isoformat() == "2026-06-02"          # target_date, not "any day"
    assert call.kwargs.get("time_of_day") == "eftermiddag"
    # And context persisted both, in case of a follow-up turn.
    saved = json.loads(conv.context_json)["booking"]
    assert saved["target_date"] == "2026-06-02"
    assert saved["time_of_day"] == "eftermiddag"


# -- BOOK-03: Confirmation creates booking + notifies owner --

@patch("app.booking.handler.OWNER_PHONE", "+4500000000")
@patch("app.booking.handler.send_sms")
@patch("app.booking.handler.build_calendar_service")
@patch("app.booking.handler.create_event")
def test_book_03_confirm_creates_booking_and_notifies_owner(
    mock_create_event, mock_build_service, mock_sms, db_session
):
    """On confirmation, Booking row created with status=confirmed, owner SMS sent."""
    mock_create_event.return_value = {"id": "gcal_abc123"}

    conv = Conversation(phone_number="+4512345678", state="booking_offer_slot")
    conv.context_json = json.dumps({
        "booking": {
            "appointment_type": "lesson",
            "offered_slot": "2026-04-15T08:15:00",
        }
    })
    db_session.add(conv)
    db_session.flush()

    response, new_state = handle_booking_step(db_session, conv, "Ja", "da")

    assert new_state == ConversationState.IDLE
    # The confirmation is now delivered via the Twilio API (send_sms), not the
    # webhook's TwiML reply, to survive the CalDAV webhook-timeout window. The
    # handler therefore returns a silent (empty) reply.
    assert response == ""

    # Verify Booking record
    booking = db_session.query(Booking).filter_by(conversation_id=conv.id).first()
    assert booking is not None
    assert booking.status == "confirmed"
    assert booking.appointment_type == "lesson"
    assert booking.gcal_event_id == "gcal_abc123"

    # Two API sends: owner notification + student confirmation
    sent = {call.args[0]: call.args[1] for call in mock_sms.call_args_list}
    assert "[LISA BOOKING]" in sent["+4500000000"]
    assert "Booket" in sent["+4512345678"]

    # Student confirmation is persisted as an outbound Message (the webhook skips
    # logging because the handler returns a silent reply).
    outbound = (
        db_session.query(Message)
        .filter_by(conversation_id=conv.id, direction="outbound")
        .all()
    )
    assert any("Booket" in m.body for m in outbound)


# -- BOOK-05: Manoeuvring and test use correct durations --

@patch("app.booking.handler.OWNER_PHONE", "+4500000000")
@patch("app.booking.handler.send_sms")
@patch("app.booking.handler.build_calendar_service")
@patch("app.booking.handler.create_event")
def test_manoeuvring_booking_uses_180min_duration(
    mock_create_event, mock_build_service, mock_sms, db_session
):
    """Manoeuvring booking end_dt is start_dt + 180 minutes."""
    mock_create_event.return_value = {"id": "gcal_man"}

    conv = Conversation(phone_number="+4512345678", state="booking_offer_slot")
    conv.context_json = json.dumps({
        "booking": {
            "appointment_type": "manoeuvring",
            "offered_slot": "2026-04-15T08:15:00",
        }
    })
    db_session.add(conv)
    db_session.flush()

    handle_booking_step(db_session, conv, "Ja", "da")

    booking = db_session.query(Booking).filter_by(conversation_id=conv.id).first()
    assert booking is not None
    delta = booking.end_dt - booking.start_dt
    assert delta == timedelta(minutes=180)


@patch("app.booking.handler.OWNER_PHONE", "+4500000000")
@patch("app.booking.handler.send_sms")
@patch("app.booking.handler.build_calendar_service")
@patch("app.booking.handler.create_event")
def test_test_booking_uses_60min_duration(
    mock_create_event, mock_build_service, mock_sms, db_session
):
    """Test booking end_dt is start_dt + 60 minutes."""
    mock_create_event.return_value = {"id": "gcal_test"}

    conv = Conversation(phone_number="+4512345678", state="booking_offer_slot")
    conv.context_json = json.dumps({
        "booking": {
            "appointment_type": "test",
            "offered_slot": "2026-04-15T08:15:00",
        }
    })
    db_session.add(conv)
    db_session.flush()

    handle_booking_step(db_session, conv, "Ja", "da")

    booking = db_session.query(Booking).filter_by(conversation_id=conv.id).first()
    assert booking is not None
    delta = booking.end_dt - booking.start_dt
    assert delta == timedelta(minutes=60)


# -- Slot formatting helper --

def test_format_slot_returns_compact_danish():
    """_format_slot produces compact string like 'tir 15/4 kl.10:15'."""
    dt = datetime(2026, 4, 15, 8, 15)  # naive UTC, Wednesday (ons) in Copenhagen (+2)
    result = _format_slot(dt)
    assert "kl." in result
    assert "/" in result
