"""booking_status intent — answer "when is my next køretime?" from the Booking
table, plus the FAQ/LLM anti-hallucination guard (no inventing personal data).

Built after a live transcript where, with no booking-lookup capability, Lisa
classified "hvornår er min næste køretime" as faq_question and the LLM
hallucinated ("tjek dit loginkort", conflating lektionsplan position with
actual bookings).
"""
from datetime import datetime, timedelta
from unittest.mock import patch

from app.booking.handler import handle_booking_status
from app.intent.classifier import ClassificationResult
from app.intent.prompts import CLASSIFY_TOOL, INTENT_CLASSIFICATION_SYSTEM_PROMPT
from app.response.generator import (
    FAQ_RESPONSE_SYSTEM_PROMPT,
    LLM_RESPONSE_SYSTEM_PROMPT,
)
from app.models import Booking, Conversation


def _conv(db, phone="+4500000004"):
    c = Conversation(phone_number=phone, state="idle", context_json="{}")
    db.add(c)
    db.flush()
    return c


def _bk(db, conv, start, status="confirmed", appt="lesson"):
    b = Booking(
        phone_number=conv.phone_number,
        conversation_id=conv.id,
        appointment_type=appt,
        start_dt=start,
        end_dt=start + timedelta(minutes=90),
        status=status,
    )
    db.add(b)
    db.flush()
    return b


# -- handle_booking_status (frozen "now" = 2026-01-01 via conftest) ----------

def test_no_bookings_da(db_session):
    msg = handle_booking_status(db_session, _conv(db_session), "da")
    assert "ingen kommende" in msg.lower()
    assert "booke" in msg.lower()


def test_no_bookings_en(db_session):
    msg = handle_booking_status(db_session, _conv(db_session), "en")
    assert "no upcoming" in msg.lower()


def test_next_booking_reported(db_session):
    conv = _conv(db_session)
    _bk(db_session, conv, datetime(2026, 3, 16, 14, 0))
    msg = handle_booking_status(db_session, conv, "da")
    assert "din næste køretime er" in msg.lower()
    assert "16/3" in msg and "14:00" in msg


def test_multiple_bookings_reports_soonest_and_counts_extra(db_session):
    conv = _conv(db_session)
    _bk(db_session, conv, datetime(2026, 3, 18, 10, 0))
    _bk(db_session, conv, datetime(2026, 3, 16, 14, 0))  # sooner
    msg = handle_booking_status(db_session, conv, "da")
    assert "16/3" in msg          # soonest first
    assert "1 mere" in msg


def test_excludes_past_and_cancelled(db_session):
    conv = _conv(db_session)
    _bk(db_session, conv, datetime(2025, 12, 1, 9, 0))                       # past
    _bk(db_session, conv, datetime(2026, 3, 16, 14, 0), status="cancelled")  # cancelled
    msg = handle_booking_status(db_session, conv, "da")
    assert "ingen kommende" in msg.lower()


# -- routing: booking_status reaches the handler (not the FAQ generator) -----

def test_manager_routes_booking_status_to_lookup(db_session):
    conv = _conv(db_session)
    _bk(db_session, conv, datetime(2026, 3, 16, 14, 0))
    with patch(
        "app.conversation.manager.classify_message",
        return_value=ClassificationResult(intent="booking_status", confidence=0.95, language="da"),
    ):
        from app.conversation.manager import handle_message
        reply = handle_message(db_session, conv, "hvornår er min næste køretime")
    assert "din næste køretime er" in reply.lower()
    assert "loginkort" not in reply.lower()


# -- guardrails baked into the prompts --------------------------------------

def test_booking_status_in_enum_and_system_prompt():
    enum = CLASSIFY_TOOL["input_schema"]["properties"]["intent"]["enum"]
    assert "booking_status" in enum
    assert "booking_status" in INTENT_CLASSIFICATION_SYSTEM_PROMPT


def test_anti_hallucination_guard_present_in_both_prompts():
    assert "loginkort" in FAQ_RESPONSE_SYSTEM_PROMPT
    assert "loginkort" in LLM_RESPONSE_SYSTEM_PROMPT
