"""Unit tests for intent classifier (REQ-006).

Tests cover confidence thresholding, API failure handling, missing tool_use block,
and language detection. All Anthropic API calls are mocked.
"""
from unittest.mock import MagicMock

from app.intent.classifier import classify_message


def _make_mock_response(intent: str, confidence: float, language: str = "da") -> MagicMock:
    """Build a mock Anthropic response with a tool_use block."""
    mock_response = MagicMock()
    mock_block = MagicMock()
    mock_block.type = "tool_use"
    mock_block.name = "classify_intent"
    mock_block.input = {
        "intent": intent,
        "confidence": confidence,
        "language": language,
    }
    mock_response.content = [mock_block]
    return mock_response


def test_high_confidence_returns_intent(mocker):
    """Confidence 0.9 for confirm_appointment -> returns that intent."""
    mock_create = mocker.patch(
        "app.intent.classifier.client.messages.create",
        return_value=_make_mock_response("confirm_appointment", 0.9),
    )

    result = classify_message("Ja, jeg kommer")
    assert result.intent == "confirm_appointment"
    assert result.confidence == 0.9
    assert result.language == "da"
    mock_create.assert_called_once()


def test_confidence_at_threshold_returns_intent(mocker):
    """Confidence exactly 0.70 -> returns the classified intent (not unknown).

    The threshold check is strict less-than: confidence < 0.7 -> unknown.
    So 0.70 is at the boundary and must NOT be forced to unknown.
    """
    mocker.patch(
        "app.intent.classifier.client.messages.create",
        return_value=_make_mock_response("location_question", 0.70),
    )

    result = classify_message("Hvor mødes vi?")
    assert result.intent == "location_question"
    assert result.confidence == 0.70


def test_low_confidence_treated_as_unknown(mocker):
    """Confidence 0.69 -> intent forced to 'unknown', confidence preserved as 0.69."""
    mocker.patch(
        "app.intent.classifier.client.messages.create",
        return_value=_make_mock_response("location_question", 0.69),
    )

    result = classify_message("Hvad sker der?")
    assert result.intent == "unknown"
    assert result.confidence == 0.69


def test_api_failure_returns_unknown(mocker):
    """If client.messages.create raises an exception, return unknown with confidence=0.0."""
    mocker.patch(
        "app.intent.classifier.client.messages.create",
        side_effect=Exception("API down"),
    )

    result = classify_message("Hvad sker der?")
    assert result.intent == "unknown"
    assert result.confidence == 0.0
    assert result.language == "da"


def test_no_tool_use_block_returns_unknown(mocker):
    """Response with only text blocks (no tool_use) -> returns unknown with confidence=0.0."""
    mock_response = MagicMock()
    mock_block = MagicMock()
    mock_block.type = "text"
    mock_block.text = "I cannot classify this"
    mock_response.content = [mock_block]

    mocker.patch(
        "app.intent.classifier.client.messages.create",
        return_value=mock_response,
    )

    result = classify_message("Noget mærkeligt")
    assert result.intent == "unknown"
    assert result.confidence == 0.0
    assert result.language == "da"


def test_language_detection(mocker):
    """Claude returns language='en' -> ClassificationResult.language == 'en'."""
    mocker.patch(
        "app.intent.classifier.client.messages.create",
        return_value=_make_mock_response("confirm_appointment", 0.9, language="en"),
    )

    result = classify_message("Yes, I will be there")
    assert result.language == "en"
    assert result.intent == "confirm_appointment"


def test_book_lesson_intent(mocker):
    """book_lesson classified at high confidence (INTNT-01)."""
    mocker.patch(
        "app.intent.classifier.client.messages.create",
        return_value=_make_mock_response("book_lesson", 0.92),
    )
    result = classify_message("Jeg vil gerne booke en køretime")
    assert result.intent == "book_lesson"
    assert result.confidence == 0.92


def test_cancel_booking_intent(mocker):
    """cancel_booking classified at high confidence (INTNT-02)."""
    mocker.patch(
        "app.intent.classifier.client.messages.create",
        return_value=_make_mock_response("cancel_booking", 0.95),
    )
    result = classify_message("Jeg er nødt til at aflyse i morgen")
    assert result.intent == "cancel_booking"
    assert result.confidence == 0.95


def test_reschedule_booking_intent(mocker):
    """reschedule_booking classified at high confidence (INTNT-03)."""
    mocker.patch(
        "app.intent.classifier.client.messages.create",
        return_value=_make_mock_response("reschedule_booking", 0.95),
    )
    result = classify_message("Kan vi rykke timen til torsdag?")
    assert result.intent == "reschedule_booking"
    assert result.confidence == 0.95


def test_old_intent_names_removed_from_enum():
    """Verify cancel_request and reschedule_request are NOT in the CLASSIFY_TOOL enum (D-01)."""
    from app.intent.prompts import CLASSIFY_TOOL
    enum_values = CLASSIFY_TOOL["input_schema"]["properties"]["intent"]["enum"]
    assert "cancel_request" not in enum_values
    assert "reschedule_request" not in enum_values
    assert "cancel_booking" in enum_values
    assert "reschedule_booking" in enum_values
    assert "book_lesson" in enum_values
