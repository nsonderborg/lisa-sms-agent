"""Phase 999.28 — Beta-allowlist gate on inbound SMS.

Locks the 6 hard invariants from the ROADMAP:

1. Empty BETA_PHONE_ALLOWLIST → current behavior unchanged (all phones pass).
2. OWNER_PHONE always bypasses the gate, regardless of allowlist contents.
3. Allowlisted phones reach the conversation/intent/LLM/DB pipeline.
4. Non-allowlisted phones get a 200 + Danish reject TwiML.
5. Non-allowlisted phones produce ZERO rows in conversations/messages/leads/bookings.
6. The reject TwiML is well-formed XML containing the verbatim Danish message.

See .planning/ROADMAP.md "Phase 999.28: Beta-allowlist gate on inbound SMS".
"""
from __future__ import annotations

import xml.etree.ElementTree as ET

import pytest

from app.models import Booking, Conversation, Lead, Message


REJECT_MESSAGE_VERBATIM = (
    "Tak for din besked. Lisa er endnu ikke åben for nye elever — "
    "kontakt skolen direkte for at booke en køretime."
)


@pytest.fixture
def bypass_twilio_signature(mocker):
    """TestClient hits 'testserver', not localhost — the localhost bypass in
    webhook.validate_twilio_request doesn't cover it. Mock outright."""
    mocker.patch("app.sms.webhook.validate_twilio_request", return_value=True)


@pytest.fixture
def stub_handle_message(mocker):
    """Stub handle_message so we don't run the full intent/LLM/booking pipeline.
    Returns a fixed reply so pipeline-reach tests can assert the path executed."""
    return mocker.patch(
        "app.sms.webhook.handle_message",
        return_value="STUB_REPLY",
    )


def _post_sms(client, *, from_phone, body="hej", message_sid="SM" + "0" * 32):
    """POST a Twilio-shaped form payload to /sms (mirrors the pressure suite helper)."""
    return client.post(
        "/sms",
        data={
            "From": from_phone,
            "Body": body,
            "MessageSid": message_sid,
            "AccountSid": "ACtest00000000000000000000000000000",
            "To": "+15005550006",
        },
    )


# ---------------------------------------------------------------------------
# Invariant 1: empty allowlist preserves current production behavior.
# ---------------------------------------------------------------------------

def test_empty_allowlist_passes_all_phones(
    client, db_session, bypass_twilio_signature, stub_handle_message, mock_send_sms, monkeypatch
):
    """Production-mode regression: with an empty BETA_PHONE_ALLOWLIST, any phone
    reaches the pipeline (handle_message is called) and a Conversation row is
    created. This is the zero-risk default that protects production deploys
    where the env var was forgotten."""
    monkeypatch.setattr("app.sms.webhook.BETA_PHONE_ALLOWLIST", frozenset())

    arbitrary_phone = "+4599999999"
    r = _post_sms(client, from_phone=arbitrary_phone, body="hej")

    assert r.status_code == 200, r.text
    assert stub_handle_message.called, "pipeline must run when allowlist is empty"
    convs = db_session.query(Conversation).filter_by(phone_number=arbitrary_phone).count()
    assert convs == 1, f"expected 1 Conversation row, got {convs}"


# ---------------------------------------------------------------------------
# Invariant 2: OWNER_PHONE always bypasses the gate.
# ---------------------------------------------------------------------------

def test_owner_phone_always_exempt_even_when_allowlisted_others(
    client, db_session, bypass_twilio_signature, stub_handle_message, mock_send_sms, monkeypatch
):
    """Owner-side commands (LISA ...) must always work for operational control
    even mid-pilot. The owner is NOT in the allowlist below, but must still
    reach the pipeline."""
    owner_phone = "+4500000000"  # matches conftest.py OWNER_PHONE
    monkeypatch.setattr("app.sms.webhook.OWNER_PHONE", owner_phone)
    monkeypatch.setattr(
        "app.sms.webhook.BETA_PHONE_ALLOWLIST",
        frozenset({"+4500000002"}),  # deliberately does NOT include owner
    )

    r = _post_sms(client, from_phone=owner_phone, body="LISA STATUS")

    assert r.status_code == 200, r.text
    assert stub_handle_message.called, "owner must always reach the pipeline"
    convs = db_session.query(Conversation).filter_by(phone_number=owner_phone).count()
    assert convs == 1


# ---------------------------------------------------------------------------
# Invariant 3: allowlisted phones reach the pipeline.
# ---------------------------------------------------------------------------

def test_allowlisted_phone_reaches_pipeline(
    client, db_session, bypass_twilio_signature, stub_handle_message, mock_send_sms, monkeypatch
):
    """Pilot student on the allowlist: full pipeline runs, Conversation created."""
    pilot_phone = "+4500000002"
    monkeypatch.setattr(
        "app.sms.webhook.BETA_PHONE_ALLOWLIST",
        frozenset({pilot_phone, "+4500000001"}),
    )

    r = _post_sms(client, from_phone=pilot_phone, body="hej")

    assert r.status_code == 200, r.text
    assert stub_handle_message.called, "allowlisted phone must reach the pipeline"
    convs = db_session.query(Conversation).filter_by(phone_number=pilot_phone).count()
    assert convs == 1


# ---------------------------------------------------------------------------
# Invariant 4 + 6: non-allowlisted phone gets a well-formed Danish reject TwiML.
# ---------------------------------------------------------------------------

def test_non_allowlisted_phone_gets_reject_twiml(
    client, db_session, bypass_twilio_signature, stub_handle_message, mock_send_sms, monkeypatch, caplog
):
    """Non-allowlisted phone: 200 status, TwiML body parses as XML, contains the
    verbatim Danish reject string, content-type is the standard TwiML media
    type, and the gate logs at INFO level with the dropped phone."""
    import logging

    monkeypatch.setattr(
        "app.sms.webhook.BETA_PHONE_ALLOWLIST",
        frozenset({"+4500000002"}),  # only one pilot phone allowed
    )
    blocked_phone = "+4599999999"

    with caplog.at_level(logging.INFO, logger="app.sms.webhook"):
        r = _post_sms(client, from_phone=blocked_phone, body="kan jeg booke?")

    assert r.status_code == 200, r.text
    assert "application/xml" in r.headers["content-type"]

    # TwiML must parse as valid XML.
    root = ET.fromstring(r.content)
    assert root.tag == "Response", f"root tag is {root.tag!r}, expected Response"
    message_el = root.find("Message")
    assert message_el is not None, "TwiML must contain a <Message> element"
    assert message_el.text == REJECT_MESSAGE_VERBATIM, (
        f"reject text mismatch:\n got: {message_el.text!r}\n exp: {REJECT_MESSAGE_VERBATIM!r}"
    )

    # Pipeline must NOT have run.
    assert not stub_handle_message.called, "handle_message must NOT be called for blocked phone"

    # Log line was emitted at INFO with the blocked phone.
    log_msgs = [rec.getMessage() for rec in caplog.records]
    assert any(
        f"Beta-gate: dropped SMS from non-allowlisted phone {blocked_phone}" in m
        for m in log_msgs
    ), f"expected gate log line, got: {log_msgs}"


# ---------------------------------------------------------------------------
# Invariant 5: non-allowlisted phone creates ZERO DB rows.
# ---------------------------------------------------------------------------

def test_non_allowlisted_phone_no_db_writes(
    client, db_session, bypass_twilio_signature, stub_handle_message, mock_send_sms, monkeypatch
):
    """Cost-control invariant: a blocked phone must NEVER create a Conversation,
    Message, Lead, or Booking row. This is the LLM-token and DB-pollution
    safety property — even if Twilio retries the same SID 10 times, nothing
    accumulates."""
    monkeypatch.setattr(
        "app.sms.webhook.BETA_PHONE_ALLOWLIST",
        frozenset({"+4500000002"}),
    )
    blocked_phone = "+4599999999"

    # Baseline: empty DB.
    assert db_session.query(Conversation).count() == 0
    assert db_session.query(Message).count() == 0
    assert db_session.query(Lead).count() == 0
    assert db_session.query(Booking).count() == 0

    # Send 3 blocked messages with distinct SIDs.
    for i in range(3):
        r = _post_sms(
            client,
            from_phone=blocked_phone,
            body=f"besked {i}",
            message_sid="SM" + str(i) * 32,
        )
        assert r.status_code == 200

    # Hard invariant: zero rows of every relevant kind.
    assert db_session.query(Conversation).count() == 0, "Conversation rows must NOT be created"
    assert db_session.query(Message).count() == 0, "Message rows must NOT be created"
    assert db_session.query(Lead).count() == 0, "Lead rows must NOT be created"
    assert db_session.query(Booking).count() == 0, "Booking rows must NOT be created"

    # And handle_message was never invoked → no LLM cost.
    assert not stub_handle_message.called, "LLM pipeline must never run for blocked phone"
