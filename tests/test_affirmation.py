"""Unit tests for natural-language affirmation detection (Phase 999.1)."""
import pytest

from app.conversation.affirmation import is_affirmation

# Plain affirmations that MUST confirm.
AFFIRMATIONS = [
    "ja", "Ja", "JA", " ja ", "  ja  ", "ja!", "ja.",
    "yes", "ok", "OK", "okay", "jep", "jo", "yep", "yup", "sure", "y",
    "ja tak", "ja, tak", "ja det passer", "ja det er fint", "ja gerne",
    "ok det passer", "lyder godt", "perfekt", "Perfekt!", "det passer fint",
    "yes please", "sounds good", "det er ok", "fint", "super",
    "👍", "ok 👍", "ja 👍", "👌",
]

# Messages that MUST NOT confirm (rejections / picks / preferences / questions).
NON_AFFIRMATIONS = [
    "", "   ",
    "nej", "no", "nej tak", "ellers nej", "stop", "annuller",
    "ja, 2", "2", "ja men tirsdag", "ja men kan jeg få en anden tid?",
    "kan jeg få en anden tid?", "hvad koster en køretime?",
    "perfekt på mandag", "ja klokken 10", "tirsdag", "eftermiddag",
    "måske", "ved ikke", "🤔", "😡",
]


@pytest.mark.parametrize("phrase", AFFIRMATIONS)
def test_is_affirmation_accepts(phrase):
    assert is_affirmation(phrase) is True, f"{phrase!r} should be an affirmation"


@pytest.mark.parametrize("phrase", NON_AFFIRMATIONS)
def test_is_affirmation_rejects(phrase):
    assert is_affirmation(phrase) is False, f"{phrase!r} should NOT be an affirmation"
