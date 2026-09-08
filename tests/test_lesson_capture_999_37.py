"""999.37 pilot seed — lektionsplan-position capture + content-label nudge.

Covers the first-booking onboarding sub-flow (name → lesson → verify nudge),
the tolerant unknown/unlabelled paths, the confirm-step correction, and the
"lektion N" surfacing on the calendar event title + owner CC.
"""
from datetime import datetime, timedelta
from unittest.mock import patch

from app.booking.handler import (
    _handle_confirm_lesson,
    _handle_intake_lesson,
    _handle_intake_name,
    _parse_lesson_number,
    _confirm_booking,
)
from app.conversation.state import ConversationState
from app.lead.capture import get_student_lesson, upsert_student_name
from app.models import Conversation, Lead


# -- _parse_lesson_number ---------------------------------------------------

def test_parse_plain_number():
    assert _parse_lesson_number("36") == 36


def test_parse_number_in_sentence():
    assert _parse_lesson_number("jeg er på lektion 18 tror jeg") == 18


def test_parse_unknown_phrase_returns_none():
    assert _parse_lesson_number("ved ikke") is None
    assert _parse_lesson_number("aner det ikke") is None
    assert _parse_lesson_number("don't know") is None


def test_parse_no_digit_returns_none():
    assert _parse_lesson_number("hej igen") is None


def test_parse_out_of_range_returns_none():
    assert _parse_lesson_number("0") is None
    assert _parse_lesson_number("99") is None


# -- name → lesson transition ----------------------------------------------

def _new_conv(db, phone="+4500000003"):
    conv = Conversation(
        phone_number=phone, state="booking_intake_name", context_json='{"booking": {}}'
    )
    db.add(conv)
    db.flush()
    return conv


def test_name_intake_routes_to_lesson_capture(db_session):
    conv = _new_conv(db_session)
    reply, state = _handle_intake_name(
        db_session, conv, "Dione", "da", {"booking": {}}
    )
    assert state == ConversationState.BOOKING_INTAKE_LESSON
    assert "Dione" in reply
    assert "kørelektion" in reply.lower()
    assert "lektionsplan" in reply.lower()


# -- lesson capture: known label → verify nudge -----------------------------

def test_known_label_triggers_verify_nudge_and_stores(db_session):
    conv = _new_conv(db_session)
    upsert_student_name(db_session, conv.phone_number, "Dione")

    reply, state = _handle_intake_lesson(
        db_session, conv, "36", "da", {"booking": {}}
    )

    assert state == ConversationState.BOOKING_CONFIRM_LESSON
    assert "rundkørsel" in reply.lower()
    assert "36" in reply
    assert get_student_lesson(db_session, conv.phone_number) == 36


def test_confirm_nudge_yes_proceeds_to_type(db_session):
    conv = _new_conv(db_session)
    upsert_student_name(db_session, conv.phone_number, "Dione")
    set = _handle_intake_lesson(db_session, conv, "36", "da", {"booking": {}})
    assert set[1] == ConversationState.BOOKING_CONFIRM_LESSON

    reply, state = _handle_confirm_lesson(
        db_session, conv, "ja", "da", {"booking": {}}
    )
    # No type stated → falls through to the type question, lesson stays 36.
    assert state == ConversationState.BOOKING_SELECT_TYPE
    assert get_student_lesson(db_session, conv.phone_number) == 36


def test_confirm_nudge_correction_updates_lesson(db_session):
    conv = _new_conv(db_session)
    upsert_student_name(db_session, conv.phone_number, "Dione")
    _handle_intake_lesson(db_session, conv, "36", "da", {"booking": {}})

    _, state = _handle_confirm_lesson(
        db_session, conv, "nej, det er 37", "da", {"booking": {}}
    )
    assert state == ConversationState.BOOKING_SELECT_TYPE
    assert get_student_lesson(db_session, conv.phone_number) == 37


# -- tolerant paths: never block -------------------------------------------

def test_unknown_lesson_stores_null_and_proceeds(db_session):
    conv = _new_conv(db_session)
    upsert_student_name(db_session, conv.phone_number, "Dione")

    _, state = _handle_intake_lesson(
        db_session, conv, "ved ikke", "da", {"booking": {}}
    )
    # No verify step — proceeds straight to the type question.
    assert state == ConversationState.BOOKING_SELECT_TYPE
    assert get_student_lesson(db_session, conv.phone_number) is None


def test_unlabelled_number_stores_and_proceeds_without_nudge(db_session):
    """A real lektion number with no road-lesson label (e.g. a theory lektion)
    is stored but skips the verify nudge — there's nothing to verify."""
    conv = _new_conv(db_session)
    upsert_student_name(db_session, conv.phone_number, "Dione")

    _, state = _handle_intake_lesson(
        db_session, conv, "25", "da", {"booking": {}}
    )
    assert state == ConversationState.BOOKING_SELECT_TYPE
    assert get_student_lesson(db_session, conv.phone_number) == 25


def test_known_lesson_resumes_to_offer_when_type_known(db_session):
    """If a type was stashed during onboarding, confirming the lesson jumps to
    the slot offer rather than the type question."""
    conv = _new_conv(db_session)
    upsert_student_name(db_session, conv.phone_number, "Dione")
    ctx = {"booking": {"appointment_type": "lesson"}}

    with patch("app.booking.handler._offer_earliest_slot") as mock_offer:
        mock_offer.return_value = ("OFFER", ConversationState.BOOKING_OFFER_SLOT)
        # unlabelled number → proceeds directly via _resume_to_offer_or_type
        _, state = _handle_intake_lesson(db_session, conv, "25", "da", ctx)

    assert state == ConversationState.BOOKING_OFFER_SLOT
    mock_offer.assert_called_once()


# -- surfacing on calendar + CC --------------------------------------------

def _confirm_setup(db, lesson):
    conv = Conversation(
        phone_number="+4500000003", state="booking_confirm", context_json="{}"
    )
    db.add(conv)
    db.flush()
    db.add(Lead(phone_number=conv.phone_number, name="Dione", current_lesson=lesson))
    db.flush()
    slot = (datetime.now() + timedelta(days=60)).replace(
        hour=10, minute=0, second=0, microsecond=0
    )
    ctx = {"booking": {"offered_slot": slot.isoformat(), "appointment_type": "lesson"}}
    return conv, ctx


@patch("app.booking.handler.cc_school_owner")
@patch("app.booking.handler.send_sms")
@patch("app.booking.handler.create_event")
@patch("app.booking.handler.build_calendar_service")
def test_lesson_appears_in_calendar_and_cc(mock_svc, mock_create, mock_sms, mock_cc, db_session):
    mock_create.return_value = {"id": "evt1"}
    conv, ctx = _confirm_setup(db_session, lesson=36)

    _confirm_booking(db_session, conv, "da", ctx)

    summary = mock_create.call_args.kwargs["summary"]
    assert "lektion 36" in summary
    cc_msg = mock_cc.call_args.args[0]
    assert "lektion 36" in cc_msg


@patch("app.booking.handler.cc_school_owner")
@patch("app.booking.handler.send_sms")
@patch("app.booking.handler.create_event")
@patch("app.booking.handler.build_calendar_service")
def test_no_lesson_suffix_when_unknown(mock_svc, mock_create, mock_sms, mock_cc, db_session):
    mock_create.return_value = {"id": "evt1"}
    conv, ctx = _confirm_setup(db_session, lesson=None)

    _confirm_booking(db_session, conv, "da", ctx)

    assert "lektion" not in mock_create.call_args.kwargs["summary"].lower()
    assert "lektion" not in mock_cc.call_args.args[0].lower()
