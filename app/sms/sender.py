import logging

from twilio.rest import Client

from app.config import (
    MAX_SMS_LENGTH,
    OWNER_PHONE,
    SCHOOL_OWNER_PHONE,
    TWILIO_ACCOUNT_SID,
    TWILIO_AUTH_TOKEN,
    TWILIO_PHONE_NUMBER,
)

logger = logging.getLogger(__name__)

client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)


def send_sms(to: str, body: str) -> str | None:
    """Send an SMS via Twilio. Returns the message SID or None on failure."""
    if len(body) > MAX_SMS_LENGTH:
        logger.warning("SMS body truncated from %d to %d chars", len(body), MAX_SMS_LENGTH)
        body = body[:MAX_SMS_LENGTH - 1] + "…"

    try:
        message = client.messages.create(
            body=body,
            from_=TWILIO_PHONE_NUMBER,
            to=to,
        )
        logger.info("Sent SMS to %s (SID: %s)", to, message.sid)
        return message.sid
    except Exception:
        logger.exception("Failed to send SMS to %s", to)
        return None


def cc_school_owner(body: str) -> None:
    """CC the school owner (SCHOOL_OWNER_PHONE) on a booking/cancellation alert.

    Additive and default-off: a no-op unless SCHOOL_OWNER_PHONE is set and
    differs from OWNER_PHONE. Lets the white-glove pilot keep OWNER_PHONE on the
    developer (full operational firehose) while the actual school owner still
    gets the bookings/cancellations they must act on, alongside the calendar
    event Lisa already writes. Never raises — send failures are logged inside
    send_sms.
    """
    if SCHOOL_OWNER_PHONE and SCHOOL_OWNER_PHONE != OWNER_PHONE:
        send_sms(SCHOOL_OWNER_PHONE, body)
