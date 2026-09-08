"""Urgency-keyword escalation — whole-word matching (2026-06-03 fix).

The old check used substring matching, so everyday Danish words that merely
*contain* a keyword falsely escalated to the owner:
  "uheldigvis" (unfortunately) ⊃ "uheld"
  "ulykkelig"  (unhappy)       ⊃ "ulykke"
  "politik"    (politics)      ⊃ "politi"

These tests lock the whole-word behaviour: false positives must NOT escalate,
genuine reports (incl. Danish inflected forms like "politiet") must.
"""
import pytest

from app.conversation.state import contains_urgency_keyword


# --- False positives that must NOT escalate (the bug we fixed) -------------

@pytest.mark.parametrize("msg", [
    "Uheldigvis kan jeg ikke nå min time i morgen",      # unfortunately
    "Jeg er så ulykkelig over at min time blev aflyst",  # unhappy
    "Jeg interesserer mig ikke for politik",             # politics
    "Det var uheldigt at jeg kom for sent",              # unfortunate
    "Kan jeg få et crash course?",                       # crash COURSE (en) — dropped from list
    "Jeg bookede den forkerte tid by accident",          # "by accident" (en) — dropped from list
])
def test_everyday_words_do_not_escalate(msg):
    assert contains_urgency_keyword(msg) is False, msg


# --- Genuine urgency that MUST escalate ------------------------------------

@pytest.mark.parametrize("msg", [
    "Jeg har været i en ulykke og kan ikke komme",   # accident
    "Politiet har taget mit kørekort",               # police (inflected: politiet)
    "Det er akut!",                                  # acute/urgent
    "Der skete et uheld på vej hjem",                # mishap/accident
    "Min ulykken i sidste uge betyder at jeg må aflyse",  # inflected: ulykken
    "There has been an emergency",                   # english
])
def test_genuine_urgency_escalates(msg):
    assert contains_urgency_keyword(msg) is True, msg


def test_empty_and_plain_messages_do_not_escalate():
    assert contains_urgency_keyword("") is False
    assert contains_urgency_keyword("Hej, jeg vil gerne booke en køretime") is False
