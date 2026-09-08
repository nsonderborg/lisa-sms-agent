"""Unit tests for app.conversation.mid_flow_faq.

These tests target the helper in isolation: classify_message and the
response generators are patched at the helper's import site so no
Claude API calls happen. The full conversation manager is NOT exercised
here — Plans 02 and 03 cover the wired-in behaviour through the
pressure-suite scenarios.
"""

from app.conversation.mid_flow_faq import (
    MAX_MID_FLOW_FAQ_PASSTHROUGHS,
    MID_FLOW_FAQ_INTENTS,
    format_restate_offer_phrase,
    get_faq_count,
    increment_faq_count,
    reset_faq_count,
    try_mid_flow_faq,
)
from app.intent.classifier import ClassificationResult


# ---------------------------------------------------------------------------
# Module-level contract sanity (Plans 02/03 depend on these names + values)
# ---------------------------------------------------------------------------

def test_contract_constants_are_stable():
    assert MAX_MID_FLOW_FAQ_PASSTHROUGHS == 2
    assert MID_FLOW_FAQ_INTENTS == frozenset(
        {"location_question", "theory_question", "faq_question"}
    )


# ---------------------------------------------------------------------------
# Classification routing
# ---------------------------------------------------------------------------

def test_faq_question_returns_answer_plus_restate_phrase_da(mocker):
    """faq_question routes through generate_faq_response and appends DA suffix."""
    mocker.patch(
        "app.conversation.mid_flow_faq.classify_message",
        return_value=ClassificationResult(
            intent="faq_question", confidence=0.9, language="da"
        ),
    )
    faq_mock = mocker.patch(
        "app.conversation.mid_flow_faq.generate_faq_response",
        return_value="En køretime koster 500 kr.",
    )
    gen_mock = mocker.patch(
        "app.conversation.mid_flow_faq.generate_response",
        return_value="<should not be called>",
    )

    result = try_mid_flow_faq(
        "hvad koster det?",
        "da",
        "",
        offered_slot_iso="2026-05-20T10:00:00",
    )

    assert result is not None
    assert "En køretime koster 500 kr." in result
    assert "Stadig i forslag:" in result
    assert "kl.10:00" in result
    assert "passer det?" in result

    faq_mock.assert_called_once()
    # generate_faq_response signature: (language, original_message, context)
    call_kwargs = faq_mock.call_args
    # accept either positional or keyword form
    if call_kwargs.args:
        assert call_kwargs.args[0] == "da"
    else:
        assert call_kwargs.kwargs.get("language") == "da"
    gen_mock.assert_not_called()


def test_location_question_routes_to_generate_response(mocker):
    """location_question hits generate_response (template-driven), not generate_faq_response."""
    mocker.patch(
        "app.conversation.mid_flow_faq.classify_message",
        return_value=ClassificationResult(
            intent="location_question", confidence=0.95, language="da"
        ),
    )
    faq_mock = mocker.patch(
        "app.conversation.mid_flow_faq.generate_faq_response",
        return_value="<should not be called>",
    )
    gen_mock = mocker.patch(
        "app.conversation.mid_flow_faq.generate_response",
        return_value="Vi mødes på Testvej 1.",
    )

    result = try_mid_flow_faq(
        "hvor mødes vi?",
        "da",
        "",
        offered_slot_iso="2026-05-20T10:00:00",
    )

    assert result is not None
    assert "Vi mødes på Testvej 1." in result
    assert "Stadig i forslag:" in result

    gen_mock.assert_called_once()
    # generate_response signature: (intent, language, original_message, context)
    call = gen_mock.call_args
    if call.args:
        assert call.args[0] == "location_question"
    else:
        assert call.kwargs.get("intent") == "location_question"
    faq_mock.assert_not_called()


def test_theory_question_routes_to_generate_response(mocker):
    """theory_question hits generate_response, not generate_faq_response."""
    mocker.patch(
        "app.conversation.mid_flow_faq.classify_message",
        return_value=ClassificationResult(
            intent="theory_question", confidence=0.95, language="da"
        ),
    )
    faq_mock = mocker.patch(
        "app.conversation.mid_flow_faq.generate_faq_response",
        return_value="<should not be called>",
    )
    gen_mock = mocker.patch(
        "app.conversation.mid_flow_faq.generate_response",
        return_value="Teoriundervisning: mandage kl. 18-20.",
    )

    result = try_mid_flow_faq(
        "hvornår er teori?",
        "da",
        "",
        offered_slot_iso="2026-05-20T10:00:00",
    )

    assert result is not None
    assert "Teoriundervisning: mandage kl. 18-20." in result
    assert "Stadig i forslag:" in result

    gen_mock.assert_called_once()
    call = gen_mock.call_args
    if call.args:
        assert call.args[0] == "theory_question"
    else:
        assert call.kwargs.get("intent") == "theory_question"
    faq_mock.assert_not_called()


def test_non_faq_intent_returns_none(mocker):
    """Non-FAQ intents (e.g. book_lesson) return None without calling generators."""
    mocker.patch(
        "app.conversation.mid_flow_faq.classify_message",
        return_value=ClassificationResult(
            intent="book_lesson", confidence=0.95, language="da"
        ),
    )
    faq_mock = mocker.patch(
        "app.conversation.mid_flow_faq.generate_faq_response",
        return_value="<nope>",
    )
    gen_mock = mocker.patch(
        "app.conversation.mid_flow_faq.generate_response",
        return_value="<nope>",
    )

    result = try_mid_flow_faq(
        "ja",
        "da",
        "",
        offered_slot_iso="2026-05-20T10:00:00",
    )

    assert result is None
    faq_mock.assert_not_called()
    gen_mock.assert_not_called()


def test_low_confidence_returns_none(mocker):
    """Defense-in-depth: even if intent is faq_question, confidence < 0.7 returns None."""
    mocker.patch(
        "app.conversation.mid_flow_faq.classify_message",
        return_value=ClassificationResult(
            intent="faq_question", confidence=0.5, language="da"
        ),
    )
    faq_mock = mocker.patch(
        "app.conversation.mid_flow_faq.generate_faq_response",
        return_value="<nope>",
    )
    gen_mock = mocker.patch(
        "app.conversation.mid_flow_faq.generate_response",
        return_value="<nope>",
    )

    result = try_mid_flow_faq(
        "?",
        "da",
        "",
        offered_slot_iso="2026-05-20T10:00:00",
    )

    assert result is None
    faq_mock.assert_not_called()
    gen_mock.assert_not_called()


# ---------------------------------------------------------------------------
# Language / phrasing
# ---------------------------------------------------------------------------

def test_english_restate_phrase(mocker):
    """EN re-state phrase uses 'Still on offer:' and 'does it work?'."""
    mocker.patch(
        "app.conversation.mid_flow_faq.classify_message",
        return_value=ClassificationResult(
            intent="faq_question", confidence=0.9, language="en"
        ),
    )
    mocker.patch(
        "app.conversation.mid_flow_faq.generate_faq_response",
        return_value="A lesson costs 500 DKK.",
    )

    result = try_mid_flow_faq(
        "what's the price?",
        "en",
        "",
        offered_slot_iso="2026-05-20T10:00:00",
    )

    assert result is not None
    assert "A lesson costs 500 DKK." in result
    assert "Still on offer:" in result
    assert "does it work?" in result
    # And NOT the Danish phrasing:
    assert "Stadig i forslag:" not in result
    assert "passer det?" not in result


def test_no_offered_slot_returns_answer_only(mocker):
    """When offered_slot_iso is None, the answer is returned alone (caller appends own re-prompt)."""
    mocker.patch(
        "app.conversation.mid_flow_faq.classify_message",
        return_value=ClassificationResult(
            intent="faq_question", confidence=0.9, language="da"
        ),
    )
    mocker.patch(
        "app.conversation.mid_flow_faq.generate_faq_response",
        return_value="En køretime koster 500 kr.",
    )

    result = try_mid_flow_faq(
        "hvad koster det?",
        "da",
        "",
        offered_slot_iso=None,
    )

    assert result == "En køretime koster 500 kr."
    # Explicitly no suffix:
    assert "Stadig i forslag:" not in result
    assert "passer det?" not in result


def test_slot_formatting_in_restate_phrase(mocker):
    """2026-05-20T10:00 (Wednesday) formats as 'ons 20/5 kl.10:00' — matches handler _format_slot."""
    mocker.patch(
        "app.conversation.mid_flow_faq.classify_message",
        return_value=ClassificationResult(
            intent="faq_question", confidence=0.9, language="da"
        ),
    )
    mocker.patch(
        "app.conversation.mid_flow_faq.generate_faq_response",
        return_value="X",
    )

    result = try_mid_flow_faq(
        "hvad koster det?",
        "da",
        "",
        offered_slot_iso="2026-05-20T10:00:00",
    )

    assert result is not None
    assert "ons 20/5 kl.10:00" in result


# ---------------------------------------------------------------------------
# format_restate_offer_phrase
# ---------------------------------------------------------------------------

def test_format_restate_offer_phrase_da_and_en():
    """Direct test of the formatter (leading space is part of the contract)."""
    da = format_restate_offer_phrase("da", "tir 15/4 kl.10:00")
    en = format_restate_offer_phrase("en", "tir 15/4 kl.10:00")

    assert da == " Stadig i forslag: tir 15/4 kl.10:00 — passer det?"
    assert en == " Still on offer: tir 15/4 kl.10:00 — does it work?"
    # Leading space lets the caller concatenate without a manual separator
    assert da.startswith(" ")
    assert en.startswith(" ")


# ---------------------------------------------------------------------------
# Counter plumbing
# ---------------------------------------------------------------------------

def test_counter_increment_and_reset():
    context: dict = {}
    assert get_faq_count(context, "booking") == 0
    assert increment_faq_count(context, "booking") == 1
    assert increment_faq_count(context, "booking") == 2
    assert get_faq_count(context, "booking") == 2

    reset_faq_count(context, "booking")
    assert get_faq_count(context, "booking") == 0


def test_counter_namespaced_per_flow_key():
    """booking and cancellation counters are independent."""
    context: dict = {}
    assert increment_faq_count(context, "booking") == 1
    assert increment_faq_count(context, "cancellation") == 1

    assert get_faq_count(context, "booking") == 1
    assert get_faq_count(context, "cancellation") == 1

    # Incrementing one must not touch the other
    assert increment_faq_count(context, "booking") == 2
    assert get_faq_count(context, "cancellation") == 1
