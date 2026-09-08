"""Tests for the greeting template + the _handle_intake_name comma/keyword
split that prevents booking content from being swallowed as part of the name."""
from app.booking.handler import _split_name_and_continuation
from app.response.templates import get_template


# --- Greeting template -----------------------------------------------------

def test_greeting_da_template_exists():
    reply = get_template("greeting", "da")
    assert reply is not None
    assert "Lisa" in reply
    assert "Hej" in reply


def test_greeting_en_template_exists():
    reply = get_template("greeting", "en")
    assert reply is not None
    assert "Lisa" in reply
    assert "Hi" in reply


# --- Name + continuation split --------------------------------------------

def test_plain_first_name_no_continuation():
    name, cont = _split_name_and_continuation("Dione")
    assert name == "Dione"
    assert cont is None


def test_plain_full_name_no_continuation():
    name, cont = _split_name_and_continuation("Anders Nielsen Hansen")
    assert name == "Anders Nielsen Hansen"
    assert cont is None


def test_comma_split_extracts_name_and_continuation():
    name, cont = _split_name_and_continuation(
        "Dione, jeg vil gerne booke køretime i morgen kl 11"
    )
    assert name == "Dione"
    assert cont == "jeg vil gerne booke køretime i morgen kl 11"


def test_keyword_split_without_comma_kl_time():
    name, cont = _split_name_and_continuation("Dione jeg vil booke kl 11")
    assert name == "Dione"
    assert cont is not None
    assert "kl 11" in cont


def test_keyword_split_tomorrow():
    name, cont = _split_name_and_continuation("John Doe i morgen")
    assert name == "John Doe"
    assert cont == "i morgen"


def test_keyword_split_weekday():
    name, cont = _split_name_and_continuation("Mette Hansen mandag")
    assert name == "Mette Hansen"
    assert cont == "mandag"


def test_trailing_comma_with_empty_tail_keeps_full_name():
    # "Dione, " with nothing after the comma — treat as plain name
    name, cont = _split_name_and_continuation("Dione,")
    assert cont is None
    # comma stays in name candidate which is OK; downstream validates
    assert "Dione" in name


def test_keyword_at_start_does_not_create_empty_name():
    # "jeg vil booke" with no name prefix — name candidate would be empty,
    # so no split should be returned (validation upstream rejects empty name)
    name, cont = _split_name_and_continuation("jeg vil booke en køretime")
    assert cont is None
    assert name == "jeg vil booke en køretime"
