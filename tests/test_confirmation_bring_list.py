"""Confirmation-reply 'what to bring' line (2026-06-03).

When a student confirms a reminder (JA), Lisa appends a per-appointment-type
"Husk at medbringe ... i morgen." line, sourced from
config.yaml appointment_types.<type>.what_to_bring. Danish only; English and
no-booking cases fall back to the plain confirmation template.
"""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.conversation.manager import _confirmation_reply
from app.models import Booking, Conversation

COPENHAGEN_TZ = ZoneInfo("Europe/Copenhagen")


def _make_conv_with_booking(db_session, appt_type: str) -> Conversation:
    conv = Conversation(phone_number="+4512345678", state="awaiting_confirmation")
    db_session.add(conv)
    db_session.flush()
    now_local = datetime.now(COPENHAGEN_TZ).replace(tzinfo=None)
    booking = Booking(
        phone_number="+4512345678",
        conversation_id=conv.id,
        appointment_type=appt_type,
        start_dt=now_local + timedelta(hours=24),
        end_dt=now_local + timedelta(hours=25, minutes=30),
        status="confirmed",
    )
    db_session.add(booking)
    db_session.flush()
    return conv


def test_lesson_confirmation_appends_bring_list(db_session):
    conv = _make_conv_with_booking(db_session, "lesson")
    reply = _confirmation_reply(db_session, conv, "da")
    assert "Husk at medbringe din lektionsplan og din kørekortansøgning i morgen." in reply


def test_test_confirmation_appends_test_bring_list(db_session):
    conv = _make_conv_with_booking(db_session, "test")
    reply = _confirmation_reply(db_session, conv, "da")
    assert "Husk at medbringe" in reply
    assert "pas eller dåbsattest" in reply


def test_no_booking_falls_back_to_plain_template(db_session):
    conv = Conversation(phone_number="+4599999999", state="awaiting_confirmation")
    db_session.add(conv)
    db_session.flush()
    reply = _confirmation_reply(db_session, conv, "da")
    assert "Husk at medbringe" not in reply
    assert "bekræftelsen" in reply.lower()


def test_english_does_not_append_danish_bring_list(db_session):
    conv = _make_conv_with_booking(db_session, "lesson")
    reply = _confirmation_reply(db_session, conv, "en")
    assert "Husk at medbringe" not in reply
