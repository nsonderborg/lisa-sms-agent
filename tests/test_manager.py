"""Unit tests for conversation manager (REQ-007).

Tests cover urgency escalation, intent routing from IDLE state, unknown intent
clarification flow, rate limiting, always-escalate intents, reclassification
after clarification, and booking flow routing. All Twilio and Anthropic calls are mocked.
"""
from unittest.mock import patch

from app.conversation.manager import handle_message
from app.conversation.state import ConversationState
from app.intent.classifier import ClassificationResult
from app.models import Conversation, Message, Escalation


def _create_conversation(db_session, state: str = ConversationState.IDLE.value):
    """Helper: create a conversation + an initial inbound message in the test DB."""
    conv = Conversation(phone_number="+4512345678", state=state)
    db_session.add(conv)
    db_session.flush()

    msg = Message(conversation_id=conv.id, direction="inbound", body="test body")
    db_session.add(msg)
    db_session.commit()
    return conv


def test_urgency_keyword_escalates(db_session, mocker):
    """Message containing 'akut' in IDLE state should trigger immediate escalation."""
    conv = _create_conversation(db_session)
    mocker.patch("app.conversation.manager.send_sms", return_value="TEST_SID")
    mocker.patch("app.conversation.manager.OWNER_PHONE", "+4500000000")

    handle_message(db_session, conv, "akut hjælp!")

    # An escalation record should be created
    assert db_session.query(Escalation).count() == 1
    escalation = db_session.query(Escalation).first()
    assert escalation.reason == "urgency_keyword"


def test_idle_classify_and_respond(db_session, mocker):
    """IDLE state + confirm_appointment at 0.9 confidence -> returns template response."""
    conv = _create_conversation(db_session)
    mocker.patch("app.conversation.manager.send_sms", return_value="TEST_SID")
    mocker.patch("app.conversation.manager.OWNER_PHONE", "+4500000000")
    mocker.patch(
        "app.conversation.manager.classify_message",
        return_value=ClassificationResult(
            intent="confirm_appointment", confidence=0.9, language="da"
        ),
    )

    response = handle_message(db_session, conv, "Ja, jeg kommer")

    # Should return a non-empty string response (template text)
    assert response is not None
    assert isinstance(response, str)
    assert len(response) > 0


def test_unknown_sets_awaiting_clarification(db_session, mocker):
    """IDLE state + unknown intent -> state changes to AWAITING_CLARIFICATION."""
    conv = _create_conversation(db_session)
    mocker.patch("app.conversation.manager.send_sms", return_value="TEST_SID")
    mocker.patch("app.conversation.manager.OWNER_PHONE", "+4500000000")
    mocker.patch(
        "app.conversation.manager.classify_message",
        return_value=ClassificationResult(
            intent="unknown", confidence=0.5, language="da"
        ),
    )

    handle_message(db_session, conv, "Hvad sker der?")

    db_session.refresh(conv)
    assert conv.state == ConversationState.AWAITING_CLARIFICATION.value


def test_second_unknown_escalates(db_session, mocker):
    """AWAITING_CLARIFICATION + unknown again -> escalation created, state reset to IDLE."""
    conv = _create_conversation(db_session, state=ConversationState.AWAITING_CLARIFICATION.value)
    mocker.patch("app.conversation.manager.send_sms", return_value="TEST_SID")
    mocker.patch("app.conversation.manager.OWNER_PHONE", "+4500000000")
    mocker.patch(
        "app.conversation.manager.classify_message",
        return_value=ClassificationResult(
            intent="unknown", confidence=0.4, language="da"
        ),
    )

    handle_message(db_session, conv, "Jeg forstår stadig ikke")

    # Escalation should have been created
    assert db_session.query(Escalation).count() == 1

    db_session.refresh(conv)
    assert conv.state == ConversationState.IDLE.value


def test_reschedule_booking_routes_to_cancellation_flow(db_session, mocker):
    """reschedule_booking intent now routes to start_cancellation_flow (not escalation).

    Since cancellation flow replaced the always-escalate path in phase 08-02,
    this verifies that a student with no bookings gets the 'no bookings' response
    (not an escalation) when they send a reschedule message.
    """
    conv = _create_conversation(db_session)
    mocker.patch("app.conversation.manager.send_sms", return_value="TEST_SID")
    mocker.patch("app.conversation.manager.OWNER_PHONE", "+4500000000")
    mocker.patch(
        "app.conversation.manager.classify_message",
        return_value=ClassificationResult(
            intent="reschedule_booking", confidence=0.9, language="da"
        ),
    )

    response = handle_message(db_session, conv, "Kan vi flytte timen?")

    # No escalation — cancellation flow handles it directly
    assert db_session.query(Escalation).count() == 0
    # Student gets informative "no bookings" response (no upcoming bookings in test DB)
    assert response is not None
    assert "ingen" in response.lower() or "no upcoming" in response.lower()


def test_clarification_success_reclassifies(db_session, mocker):
    """AWAITING_CLARIFICATION + classified intent (not unknown) -> state reset to IDLE, returns response."""
    conv = _create_conversation(db_session, state=ConversationState.AWAITING_CLARIFICATION.value)
    mocker.patch("app.conversation.manager.send_sms", return_value="TEST_SID")
    mocker.patch("app.conversation.manager.OWNER_PHONE", "+4500000000")
    mocker.patch(
        "app.conversation.manager.classify_message",
        return_value=ClassificationResult(
            intent="confirm_appointment", confidence=0.9, language="da"
        ),
    )

    response = handle_message(db_session, conv, "Ja, jeg bekræfter")

    db_session.refresh(conv)
    assert conv.state == ConversationState.IDLE.value
    # Should return a valid response string
    assert response is not None
    assert isinstance(response, str)


@patch("app.conversation.manager.start_booking_flow")
@patch("app.conversation.manager.classify_message")
@patch("app.conversation.manager.send_sms")
@patch("app.conversation.manager.OWNER_PHONE", "+4500000000")
def test_book_lesson_routes_to_booking_flow(
    mock_sms, mock_classify, mock_start_booking, db_session
):
    """book_lesson intent routes to start_booking_flow instead of escalation."""
    mock_classify.return_value = ClassificationResult(
        intent="book_lesson", confidence=0.9, language="da"
    )
    mock_start_booking.return_value = (
        "Hvad vil du booke? Koretime, manoevrebane eller prove?",
        ConversationState.BOOKING_SELECT_TYPE,
    )

    conv = Conversation(phone_number="+4599999999", state="idle")
    db_session.add(conv)
    db_session.flush()

    result = handle_message(db_session, conv, "Jeg vil booke")

    mock_start_booking.assert_called_once()
    assert result is not None


@patch("app.conversation.manager.handle_booking_step")
@patch("app.conversation.manager.send_sms")
@patch("app.conversation.manager.OWNER_PHONE", "+4500000000")
def test_booking_state_routes_to_handler(
    mock_sms, mock_booking_step, db_session
):
    """Messages in booking flow states route to handle_booking_step."""
    mock_booking_step.return_value = (
        "Naeste ledige tid er tir 15/4 kl.10:15.",
        ConversationState.BOOKING_OFFER_SLOT,
    )

    conv = Conversation(phone_number="+4599999999", state="booking_select_type")
    db_session.add(conv)
    db_session.flush()

    handle_message(db_session, conv, "koretime")

    mock_booking_step.assert_called_once()


def test_emotional_unknown_acknowledges_and_escalates(db_session, mocker):
    """Phase 999.35: a nervous opener (unknown + emotional) gets a warm
    acknowledgment + a distinctly-tagged escalation, not the flat re-prompt."""
    conv = _create_conversation(db_session)
    send = mocker.patch("app.conversation.manager.send_sms", return_value="TEST_SID")
    mocker.patch("app.conversation.manager.OWNER_PHONE", "+4500000000")
    mocker.patch(
        "app.conversation.manager.classify_message",
        return_value=ClassificationResult(
            intent="unknown", confidence=0.4, language="da", emotional=True
        ),
    )

    response = handle_message(db_session, conv, "Jeg er nervøs for at køre")

    # Warm acknowledgment, not the flat clarification re-prompt.
    assert "nervøs" in response.lower()
    assert "gode hænder" in response.lower()
    # Escalated with the emotional-specific reason, and NOT left awaiting clarification.
    esc = db_session.query(Escalation).one()
    assert esc.reason == "emotional_distress"
    db_session.refresh(conv)
    assert conv.state == ConversationState.IDLE.value
    # Owner alert carries the distinct emotional tag.
    owner_to, owner_body = send.call_args[0]
    assert owner_to == "+4500000000"
    assert "ELEV USIKKER" in owner_body


def test_non_emotional_unknown_uses_clarification_path(db_session, mocker):
    """A non-emotional unknown still routes to the clarification re-prompt."""
    conv = _create_conversation(db_session)
    mocker.patch("app.conversation.manager.send_sms", return_value="TEST_SID")
    mocker.patch("app.conversation.manager.OWNER_PHONE", "+4500000000")
    mocker.patch(
        "app.conversation.manager.classify_message",
        return_value=ClassificationResult(
            intent="unknown", confidence=0.3, language="da", emotional=False
        ),
    )

    handle_message(db_session, conv, "Hvad sker der?")

    db_session.refresh(conv)
    assert conv.state == ConversationState.AWAITING_CLARIFICATION.value
