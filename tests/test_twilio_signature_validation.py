"""Twilio signature validation and its dev-mode escape hatch (security F3).

The bypass used to trigger on ``request.url.hostname in ["localhost", "127.0.0.1"]``.
That hostname comes from the client-supplied ``Host`` header, so anything able to
reach the process directly could spoof it and skip validation entirely. It is now
gated on ``LISA_SKIP_TWILIO_SIGNATURE``, which is never set in production.
"""
from unittest.mock import MagicMock

import pytest

from app.sms import webhook


def _request(host: str = "lisa.example.com", signature: str = "") -> MagicMock:
    """A stand-in for the parts of fastapi.Request that validation reads."""
    request = MagicMock()
    request.url = MagicMock()
    request.url.hostname = host
    request.url.__str__ = lambda self: f"https://{host}/sms"
    request.headers = {"X-Twilio-Signature": signature}
    return request


@pytest.fixture(autouse=True)
def _no_dev_mode(monkeypatch):
    """Production-like default: the escape hatch is off unless a test opts in."""
    monkeypatch.delenv("LISA_SKIP_TWILIO_SIGNATURE", raising=False)


@pytest.mark.parametrize("spoofed_host", ["localhost", "127.0.0.1"])
def test_spoofed_localhost_host_header_does_not_bypass(monkeypatch, spoofed_host):
    """THE regression test for F3: a forged Host header must not skip validation.

    An unsigned request claiming to come from localhost has to fall through to
    the real validator and be rejected — otherwise anyone reaching the process
    directly can inject arbitrary inbound SMS.
    """
    monkeypatch.setattr(webhook.validator, "validate", lambda url, body, sig: False)

    assert webhook.validate_twilio_request(_request(host=spoofed_host), {}) is False


def test_dev_mode_env_var_bypasses(monkeypatch):
    """The legitimate local-development path, which no attacker can set remotely."""
    monkeypatch.setenv("LISA_SKIP_TWILIO_SIGNATURE", "1")
    monkeypatch.setattr(webhook.validator, "validate", lambda url, body, sig: False)

    assert webhook.validate_twilio_request(_request(), {}) is True


@pytest.mark.parametrize("value", ["0", "true", "yes", ""])
def test_dev_mode_only_accepts_exactly_one(monkeypatch, value):
    """Fail closed on anything but "1" — a stray truthy-looking value in a
    production environment must not silently disable signature checking."""
    monkeypatch.setenv("LISA_SKIP_TWILIO_SIGNATURE", value)
    monkeypatch.setattr(webhook.validator, "validate", lambda url, body, sig: False)

    assert webhook.validate_twilio_request(_request(), {}) is False


def test_valid_signature_passes_through():
    """A correctly signed request still validates normally."""
    seen = {}

    def _validate(url, body, sig):
        seen["url"], seen["sig"] = url, sig
        return True

    webhook.validator.validate, original = _validate, webhook.validator.validate
    try:
        assert webhook.validate_twilio_request(_request(signature="sig123"), {}) is True
    finally:
        webhook.validator.validate = original

    assert seen["sig"] == "sig123"
    assert seen["url"].startswith("https://")


def test_forwarded_proto_rewrites_scheme_for_signing(monkeypatch):
    """Behind Railway's proxy the app sees http://; Twilio signed https://, so the
    scheme must be rewritten before validating or every real request 403s."""
    seen = {}
    monkeypatch.setattr(
        webhook.validator, "validate",
        lambda url, body, sig: seen.setdefault("url", url) or True,
    )

    request = _request()
    request.url.__str__ = lambda self: "http://lisa.example.com/sms"
    request.headers = {"X-Twilio-Signature": "s", "x-forwarded-proto": "https"}

    webhook.validate_twilio_request(request, {})
    assert seen["url"] == "https://lisa.example.com/sms"
