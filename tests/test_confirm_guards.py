"""Confirm-time guards for the booking + reschedule flows.

Two defence-in-depth re-checks at the moment a student's "ja" creates a Booking
(the offer-side filters run earlier; these catch what changed between offer and
confirm):

  Finding 1 — past-slot guard: the offered slot must still be in the future at
  confirm time. The offer-side `get_slots_for_date` filter only guarantees
  future-ness when the slot was OFFERED; a student who delays their "ja" can
  otherwise confirm a slot that has since passed (same class as e428c5d, at the
  confirm step). Uses a patchable `_now_local()` seam so the time-travelling
  pressure suite (which mocks finders with fixed May-2026 dates) is unaffected.

  Finding 2 — calendar-aware conflict: the re-check unions ALL busy sources
  (confirmed Bookings AND cached owner-calendar events across all calendars per
  999.30), not just the Booking table — so an owner adding a real commitment over
  the offered slot between offer and confirm is caught.
"""
import json
from datetime import datetime
from zoneinfo import ZoneInfo

from app.booking.handler import handle_booking_step
from app.conversation.state import ConversationState
from app.models import Booking, CalendarCache, Conversation

COPENHAGEN_TZ = ZoneInfo("Europe/Copenhagen")


def _make_offer_conv(db_session, offered_slot: datetime, appt: str = "lesson") -> Conversation:
    conv = Conversation(
        phone_number="+4500000001",
        state=ConversationState.BOOKING_OFFER_SLOT.value,
    )
    conv.context_json = json.dumps(
        {"booking": {"appointment_type": appt, "offered_slot": offered_slot.isoformat()}}
    )
    db_session.add(conv)
    db_session.commit()
    return conv


def _mock_side_effects(mocker):
    mocker.patch("app.booking.handler.send_sms")
    mocker.patch("app.booking.handler.build_calendar_service")
    mocker.patch("app.booking.handler.create_event", return_value={"id": "evt-x"})
    mocker.patch("app.booking.handler.get_student_name", return_value=None)


# ---------------------------------------------------------------------------
# Finding 1 — past-slot guard at confirm
# ---------------------------------------------------------------------------

def test_confirm_rejects_past_slot(db_session, mocker):
    """A slot that has passed since it was offered must NOT create a Booking."""
    _mock_side_effects(mocker)
    slot = datetime(2026, 5, 20, 10, 0)  # naive Copenhagen local
    # Freeze the guard's clock to AFTER the offered slot.
    mocker.patch("app.booking.handler._now_local", return_value=datetime(2026, 5, 20, 11, 0))

    conv = _make_offer_conv(db_session, slot)
    reply, state = handle_booking_step(db_session, conv, "ja", "da")

    assert db_session.query(Booking).count() == 0
    assert state == ConversationState.IDLE
    assert "passeret" in reply.lower()


def test_confirm_allows_future_slot(db_session, mocker):
    """Guard must not over-fire: a still-future slot confirms normally."""
    _mock_side_effects(mocker)
    slot = datetime(2026, 5, 20, 10, 0)
    mocker.patch("app.booking.handler._now_local", return_value=datetime(2026, 5, 20, 9, 0))

    conv = _make_offer_conv(db_session, slot)
    reply, state = handle_booking_step(db_session, conv, "ja", "da")

    assert db_session.query(Booking).filter_by(status="confirmed").count() == 1
    assert state == ConversationState.IDLE


# ---------------------------------------------------------------------------
# Finding 2 — calendar-aware conflict at confirm
# ---------------------------------------------------------------------------

def test_confirm_rejects_slot_overlapping_owner_calendar(db_session, mocker):
    """A slot overlapping a cached owner-calendar event (NO Booking row) must be
    rejected at confirm — not just Booking-table conflicts."""
    _mock_side_effects(mocker)
    mocker.patch("app.booking.handler._now_local", return_value=datetime(2026, 5, 20, 9, 0))
    slot = datetime(2026, 5, 20, 10, 0)  # local -> lesson 10:00-11:30 local

    # CalendarCache stores naive UTC. 2026-05-20 is CEST (UTC+2), so local
    # 10:00-11:30 == 08:00-09:30 UTC. This owner event overlaps the lesson.
    db_session.add(
        CalendarCache(
            event_id="owner-evt",
            summary="",
            start_dt=datetime(2026, 5, 20, 8, 0),
            end_dt=datetime(2026, 5, 20, 9, 30),
        )
    )
    db_session.commit()

    conv = _make_offer_conv(db_session, slot)
    reply, state = handle_booking_step(db_session, conv, "ja", "da")

    assert db_session.query(Booking).filter_by(status="confirmed").count() == 0
    assert "taget" in reply.lower()


def test_confirm_allows_slot_when_calendar_free(db_session, mocker):
    """Control: a cached owner event that does NOT overlap leaves the slot bookable."""
    _mock_side_effects(mocker)
    mocker.patch("app.booking.handler._now_local", return_value=datetime(2026, 5, 20, 9, 0))
    slot = datetime(2026, 5, 20, 10, 0)

    # Owner event at 06:00-07:00 UTC == 08:00-09:00 local — ends before the lesson.
    db_session.add(
        CalendarCache(
            event_id="owner-evt-early",
            summary="",
            start_dt=datetime(2026, 5, 20, 6, 0),
            end_dt=datetime(2026, 5, 20, 7, 0),
        )
    )
    db_session.commit()

    conv = _make_offer_conv(db_session, slot)
    reply, state = handle_booking_step(db_session, conv, "ja", "da")

    assert db_session.query(Booking).filter_by(status="confirmed").count() == 1


# ---------------------------------------------------------------------------
# Finding 1 — past-slot guard at reschedule confirm
# ---------------------------------------------------------------------------

def test_reschedule_confirm_rejects_past_slot_keeps_original(db_session, mocker):
    """If the new reschedule slot has passed by the final confirm, no new booking
    is created and the student's original booking is restored to confirmed."""
    from app.cancellation.handler import handle_cancellation_step

    mocker.patch("app.cancellation.handler.send_sms")
    mocker.patch("app.cancellation.handler.build_calendar_service")
    mocker.patch("app.cancellation.handler.create_event")
    mocker.patch("app.cancellation.handler.delete_event")
    mocker.patch("app.cancellation.handler.get_student_name", return_value=None)

    new_slot = datetime(2026, 5, 20, 10, 0)
    # Freeze the guard clock AFTER the new slot — it has "passed".
    mocker.patch("app.cancellation.handler._now_local", return_value=datetime(2026, 5, 20, 11, 0))

    conv = Conversation(
        phone_number="+4500000002",
        state=ConversationState.RESCHEDULE_CONFIRM.value,
    )
    db_session.add(conv)
    db_session.commit()
    old_booking = Booking(
        phone_number=conv.phone_number,
        conversation_id=conv.id,
        appointment_type="lesson",
        start_dt=datetime(2026, 5, 25, 9, 0),
        end_dt=datetime(2026, 5, 25, 10, 30),
        status="reschedule_pending",
    )
    db_session.add(old_booking)
    db_session.commit()
    conv.context_json = json.dumps(
        {
            "cancellation": {
                "intent": "reschedule_booking",
                "booking_id": old_booking.id,
                "appointment_type": "lesson",
                "offered_slot": new_slot.isoformat(),
            }
        }
    )
    db_session.commit()

    reply, state = handle_cancellation_step(db_session, conv, "ja", "da")

    assert state == ConversationState.IDLE
    assert "passeret" in reply.lower()
    # No NEW confirmed booking; original restored to confirmed.
    assert db_session.query(Booking).filter_by(status="confirmed").count() == 1
    db_session.refresh(old_booking)
    assert old_booking.status == "confirmed"
