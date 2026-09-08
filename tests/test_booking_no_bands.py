"""Phase 999.34 — drop the time-band menu.

A fluffy booking request (no time given) must now lead with the EARLIEST
slot(s) directly instead of asking "Morgen (7-9), formiddag (9-12)...". The
student steers with a day and/or a time preference as free text; the band
vocabulary ("eftermiddag") is still understood when volunteered, but Lisa never
presents the band menu as a question.
"""
import json

from datetime import datetime

from app.booking.handler import handle_booking_step, start_booking_flow
from app.conversation.state import ConversationState
from app.models import Conversation


def _prefs(appointment_type=None, days=None, time_of_day=None, preferred_time=None):
    from app.booking.preference_extractor import BookingPreferences

    return BookingPreferences(
        appointment_type=appointment_type,
        days=days or [],
        time_of_day=time_of_day,
        preferred_time=preferred_time,
        target_date=None,
        earliest_on_day=False,
        latest_on_day=False,
        date_range_start=None,
        date_range_end=None,
    )


def _make_conv(db_session, state=ConversationState.IDLE, ctx=None):
    conv = Conversation(phone_number="+4500000010", state=state.value)
    if ctx is not None:
        conv.context_json = json.dumps(ctx)
    db_session.add(conv)
    db_session.commit()
    return conv


# ---------------------------------------------------------------------------
# Fluffy entry → earliest slots, never the band menu
# ---------------------------------------------------------------------------

def test_fluffy_booking_offers_slots_not_band(db_session, mocker):
    """No time given → offer earliest slots directly, NOT the band question."""
    mocker.patch("app.booking.handler.get_student_name", return_value="Test")
    mocker.patch("app.booking.handler.extract_preferences", return_value=_prefs("lesson"))
    mocker.patch(
        "app.booking.handler.find_earliest_slots",
        return_value=[
            datetime(2026, 5, 20, 10, 0),
            datetime(2026, 5, 20, 11, 0),
            datetime(2026, 5, 21, 9, 0),
        ],
    )

    conv = _make_conv(db_session)
    reply, state = start_booking_flow(db_session, conv, "jeg vil booke en køretime", "da")

    assert state == ConversationState.BOOKING_OFFER_SLOT
    assert "Morgen (7-9)" not in reply
    assert "formiddag (9-12)" not in reply
    # Offered slots were persisted and surfaced.
    assert "10:00" in reply


def test_fluffy_single_offer_invites_day_and_time_steer(db_session, mocker):
    """A single earliest offer teaches both steer axes — a day AND a time."""
    mocker.patch("app.booking.handler.get_student_name", return_value="Test")
    mocker.patch("app.booking.handler.extract_preferences", return_value=_prefs("lesson"))
    mocker.patch(
        "app.booking.handler.find_earliest_slots",
        return_value=[datetime(2026, 5, 20, 10, 0)],
    )

    conv = _make_conv(db_session)
    reply, state = start_booking_flow(db_session, conv, "jeg vil booke en køretime", "da")

    assert state == ConversationState.BOOKING_OFFER_SLOT
    assert "Næste ledige tid er" in reply
    low = reply.lower()
    assert "dag" in low          # day-steer hint (e.g. "en dag (\"tirsdag\")")
    assert "tidspunkt" in low    # time-steer hint
    assert "Morgen (7-9)" not in reply


# ---------------------------------------------------------------------------
# Steering with a day only → search those days, no band menu
# ---------------------------------------------------------------------------

def test_days_only_steer_searches_without_band(db_session, mocker):
    """Replying with a day (no time) re-searches those days and offers slots —
    it must NOT bounce the student to the band menu."""
    mocker.patch("app.booking.handler.try_mid_flow_faq", return_value=None)
    mocker.patch("app.booking.handler.extract_preferences", return_value=_prefs(days=["tirsdag"]))
    mocker.patch(
        "app.booking.handler.find_slots_for_preferences",
        return_value=[datetime(2026, 5, 26, 10, 0), datetime(2026, 5, 26, 11, 0)],
    )

    ctx = {"booking": {"appointment_type": "lesson", "offered_slot": datetime(2026, 5, 20, 10, 0).isoformat()}}
    conv = _make_conv(db_session, state=ConversationState.BOOKING_OFFER_SLOT, ctx=ctx)

    reply, state = handle_booking_step(db_session, conv, "tirsdag", "da")

    assert state == ConversationState.BOOKING_OFFER_SLOT
    assert "Morgen (7-9)" not in reply
    assert "formiddag (9-12)" not in reply
    assert "10:00" in reply  # offered a real slot from the day search
