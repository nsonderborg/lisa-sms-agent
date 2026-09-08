"""Darkness driving (mørkekørsel / lektion 44) is NOT auto-booked — Lisa deflects
to manual arrangement with the instructor and notifies the owner.

Decision 2026-06-16 (the owner): darkness is evening/sunset-dependent, he books it by
hand. The pilot student is late in her plan, so this is live, not hypothetical.
Triggers: an explicit darkness keyword at booking entry, and a captured/corrected
lesson number in MANUAL_BOOKING_LESSONS (default {44}).
"""
from unittest.mock import patch

from app.booking.handler import (
    _handle_confirm_lesson,
    _handle_intake_lesson,
    start_booking_flow,
)
from app.conversation.state import ConversationState
from app.lead.capture import get_student_lesson, upsert_student_name
from app.models import Conversation


def _conv(db, state="booking_intake_lesson", phone="+4500000003"):
    c = Conversation(phone_number=phone, state=state, context_json='{"booking": {}}')
    db.add(c)
    db.flush()
    return c


@patch("app.booking.handler.cc_school_owner")
def test_keyword_at_entry_deflects(mock_cc, db_session):
    conv = _conv(db_session, state="idle")
    reply, state = start_booking_flow(db_session, conv, "jeg vil booke mørkekørsel", "da")
    assert state == ConversationState.IDLE
    assert "mørkekørsel" in reply.lower()
    assert "kørelærer" in reply.lower()
    assert mock_cc.called  # owner notified


@patch("app.booking.handler.cc_school_owner")
def test_natkorsel_keyword_variant_deflects(mock_cc, db_session):
    conv = _conv(db_session, state="idle")
    reply, state = start_booking_flow(db_session, conv, "kan jeg booke natkørsel?", "da")
    assert state == ConversationState.IDLE
    assert mock_cc.called


@patch("app.booking.handler.cc_school_owner")
def test_lesson_44_at_capture_deflects_and_stores(mock_cc, db_session):
    conv = _conv(db_session)
    upsert_student_name(db_session, conv.phone_number, "Dione")
    reply, state = _handle_intake_lesson(db_session, conv, "44", "da", {"booking": {}})
    assert state == ConversationState.IDLE
    assert "44" in reply and "mørkekørsel" in reply.lower()
    assert get_student_lesson(db_session, conv.phone_number) == 44  # still recorded
    assert mock_cc.called


@patch("app.booking.handler.cc_school_owner")
def test_non_manual_lesson_still_proceeds(mock_cc, db_session):
    """A labelled-but-bookable lesson (e.g. 36 rundkørsel) is unaffected."""
    conv = _conv(db_session)
    upsert_student_name(db_session, conv.phone_number, "Dione")
    reply, state = _handle_intake_lesson(db_session, conv, "36", "da", {"booking": {}})
    assert state == ConversationState.BOOKING_CONFIRM_LESSON  # normal verify nudge
    assert not mock_cc.called


@patch("app.booking.handler.cc_school_owner")
def test_correction_to_44_deflects(mock_cc, db_session):
    conv = _conv(db_session, state="booking_confirm_lesson")
    upsert_student_name(db_session, conv.phone_number, "Dione")
    reply, state = _handle_confirm_lesson(db_session, conv, "nej, det er 44", "da", {"booking": {}})
    assert state == ConversationState.IDLE
    assert "mørkekørsel" in reply.lower()
    assert get_student_lesson(db_session, conv.phone_number) == 44
    assert mock_cc.called


@patch("app.booking.handler.cc_school_owner")
def test_english_deflection(mock_cc, db_session):
    conv = _conv(db_session, state="idle")
    reply, state = start_booking_flow(db_session, conv, "I want to book night driving", "en")
    assert state == ConversationState.IDLE
    assert "darkness driving" in reply.lower()
    assert "instructor" in reply.lower()
