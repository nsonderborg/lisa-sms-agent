"""Tests for FAQ intent classification, LLM response generation, pricing soft prompt, non-FAQ escalation."""
from unittest.mock import MagicMock


# --- Tests for generate_faq_response ---

def test_faq_response_uses_llm(mocker):
    """generate_faq_response calls Claude API with FAQ context in system prompt."""
    mock_client = mocker.patch("app.response.generator.client")
    mock_response = MagicMock()
    mock_response.content = [MagicMock(text="Svar her")]
    mock_client.messages.create.return_value = mock_response
    mocker.patch("app.response.generator.FAQ", {"pricing": "14500 kr"})
    mocker.patch("app.response.generator.SCHOOL_NAME", "TestSkole")

    from app.response.generator import generate_faq_response
    generate_faq_response("da", "Hvad koster det?")

    assert mock_client.messages.create.called
    call_kwargs = mock_client.messages.create.call_args
    assert "14500 kr" in call_kwargs.kwargs["system"] or "14500 kr" in str(call_kwargs)


def test_pricing_soft_prompt(mocker):
    """Pricing FAQ response LLM prompt includes soft prompt instruction."""
    mock_client = mocker.patch("app.response.generator.client")
    mock_response = MagicMock()
    mock_response.content = [MagicMock(text="14500 kr. Vil du hore mere om at komme i gang?")]
    mock_client.messages.create.return_value = mock_response
    mocker.patch("app.response.generator.FAQ", {"pricing": "14500 kr"})

    from app.response.generator import generate_faq_response
    generate_faq_response("da", "Hvad koster korekort?")

    call_kwargs = mock_client.messages.create.call_args
    system_prompt = call_kwargs.kwargs.get("system", str(call_kwargs))
    assert "Vil du hore mere om at komme i gang?" in system_prompt or "komme i gang" in system_prompt


def test_non_pricing_no_soft_prompt(mocker):
    """Non-pricing FAQ does NOT include the soft prompt instruction."""
    mock_client = mocker.patch("app.response.generator.client")
    mock_response = MagicMock()
    mock_response.content = [MagicMock(text="3-6 maneder")]
    mock_client.messages.create.return_value = mock_response
    mocker.patch("app.response.generator.FAQ", {"duration": "3-6 mdr"})

    from app.response.generator import generate_faq_response
    generate_faq_response("da", "Hvor lang tid tager det?")

    call_kwargs = mock_client.messages.create.call_args
    system_prompt = call_kwargs.kwargs.get("system", "")
    assert "komme i gang" not in system_prompt


def test_empty_faq_no_error(mocker):
    """Empty FAQ entries don't cause KeyError."""
    mock_client = mocker.patch("app.response.generator.client")
    mock_response = MagicMock()
    mock_response.content = [MagicMock(text="Vi vender tilbage")]
    mock_client.messages.create.return_value = mock_response
    mocker.patch("app.response.generator.FAQ", {})

    from app.response.generator import generate_faq_response
    result = generate_faq_response("da", "Hvad koster det?")
    assert isinstance(result, str)


def test_faq_response_max_length(mocker):
    """Response truncated if exceeds MAX_SMS_LENGTH."""
    mock_client = mocker.patch("app.response.generator.client")
    long_text = "x" * 400
    mock_response = MagicMock()
    mock_response.content = [MagicMock(text=long_text)]
    mock_client.messages.create.return_value = mock_response
    mocker.patch("app.response.generator.FAQ", {"pricing": "test"})

    from app.response.generator import generate_faq_response
    result = generate_faq_response("da", "test")
    assert len(result) <= 320


def test_faq_intent_in_classify_tool():
    """faq_question is in the CLASSIFY_TOOL enum."""
    from app.intent.prompts import CLASSIFY_TOOL
    enum_values = CLASSIFY_TOOL["input_schema"]["properties"]["intent"]["enum"]
    assert "faq_question" in enum_values


def test_faq_intent_in_system_prompt():
    """faq_question is described in the system prompt."""
    from app.intent.prompts import INTENT_CLASSIFICATION_SYSTEM_PROMPT
    assert "faq_question" in INTENT_CLASSIFICATION_SYSTEM_PROMPT


def test_non_faq_escalates(mocker, db_session):
    """Non-FAQ unknown intent still escalates via manager."""
    # This is an integration test using the manager routing
    from app.conversation.manager import _route_by_intent
    from app.intent.classifier import ClassificationResult
    from app.models import Conversation, Message

    mocker.patch("app.conversation.manager.send_sms")
    mocker.patch("app.conversation.manager.OWNER_PHONE", "+4599999999")

    conv = Conversation(phone_number="+4511111111", state="idle")
    db_session.add(conv)
    db_session.commit()

    msg = Message(conversation_id=conv.id, direction="inbound", body="random gibberish xyz")
    db_session.add(msg)
    db_session.commit()

    result = ClassificationResult(intent="unknown", confidence=0.3, language="da")
    response = _route_by_intent(db_session, conv, "random gibberish xyz", result, "")

    # Should have gone through _handle_unknown -> clarification
    assert response is not None


def test_faq_fallback_on_api_error(mocker):
    """generate_faq_response returns fallback text when Claude API fails."""
    mock_client = mocker.patch("app.response.generator.client")
    mock_client.messages.create.side_effect = Exception("API error")
    mocker.patch("app.response.generator.FAQ", {"pricing": "14500 kr"})

    from app.response.generator import generate_faq_response
    result = generate_faq_response("da", "Hvad koster det?")
    assert "sporgsmal" in result.lower() or "Tak" in result


def test_faq_fallback_english_on_api_error(mocker):
    """generate_faq_response returns English fallback when language is en and API fails."""
    mock_client = mocker.patch("app.response.generator.client")
    mock_client.messages.create.side_effect = Exception("API error")
    mocker.patch("app.response.generator.FAQ", {})

    from app.response.generator import generate_faq_response
    result = generate_faq_response("en", "How much does it cost?")
    assert "question" in result.lower() or "Thanks" in result
