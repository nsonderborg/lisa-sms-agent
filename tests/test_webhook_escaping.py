"""Regression (Phase 999.33): the /sms webhook delivers replies via the Twilio
API, not by embedding them in the TwiML HTTP response.

This (a) makes delivery immune to the ~15s webhook timeout that used to silently
drop intermediate flow replies (slot offers, type prompts, FAQ answers), and
(b) removes the XML-escaping failure mode — a reply containing &, <, or >
(common in FAQ/LLM output, e.g. "7-9 & 9-12") used to produce malformed TwiML
that Twilio rejected (error 12200) and dropped while the app still logged the
reply. Sent via the API, the raw text needs no escaping and is delivered
verbatim, and the HTTP response carries no message body at all.
"""
import xml.etree.ElementTree as ET

import pytest


@pytest.fixture
def _mute_startup_poll(mocker):
    """Stop the app-startup calendar poll (main.lifespan) from making a real
    backend call when the TestClient boots the app. Must be set up before the
    `client` fixture triggers lifespan, so it's listed first in the test."""
    mocker.patch("app.main.poll_calendar", return_value=0)


def test_reply_delivered_via_api_verbatim(_mute_startup_poll, client, mocker):
    reply = "Tider 7-9 & 9-12; pris <500 kr> ok?"
    mocker.patch("app.sms.webhook.validate_twilio_request", return_value=True)
    mocker.patch("app.sms.webhook.handle_message", return_value=reply)
    send = mocker.patch("app.sms.webhook.send_sms", return_value="SMxxx")

    resp = client.post(
        "/sms",
        data={"From": "+4512345678", "Body": "hej", "MessageSid": "SMxmlesc1"},
    )

    assert resp.status_code == 200
    # The reply is delivered via the API, raw — no escaping, no mangling.
    send.assert_called_once_with("+4512345678", reply)

    # The HTTP response is an empty TwiML ack with NO <Message> body, so XML
    # specials in a reply can never break the response document again.
    root = ET.fromstring(resp.content)
    assert root.find("Message") is None


def test_silent_reply_sends_nothing(_mute_startup_poll, client, mocker):
    """An empty reply (rate-limited, or a confirm handler that already API-sent
    and returned "") must NOT trigger a second API send."""
    mocker.patch("app.sms.webhook.validate_twilio_request", return_value=True)
    mocker.patch("app.sms.webhook.handle_message", return_value="")
    send = mocker.patch("app.sms.webhook.send_sms", return_value=None)

    resp = client.post(
        "/sms",
        data={"From": "+4512345678", "Body": "hej", "MessageSid": "SMsilent1"},
    )

    assert resp.status_code == 200
    send.assert_not_called()
    root = ET.fromstring(resp.content)
    assert root.find("Message") is None
