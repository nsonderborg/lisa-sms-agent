import logging
import os
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, Request, Response
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from twilio.request_validator import RequestValidator

from app.config import BETA_PHONE_ALLOWLIST, OWNER_PHONE, TWILIO_AUTH_TOKEN
from app.conversation.manager import handle_message
from app.database import get_db
from app.models import Conversation, Message
from app.sms.sender import send_sms

logger = logging.getLogger(__name__)
router = APIRouter()

validator = RequestValidator(TWILIO_AUTH_TOKEN)

EMPTY_TWIML = b'<?xml version="1.0" encoding="UTF-8"?><Response></Response>'


def _beta_reject_twiml() -> bytes:
    """TwiML reply sent to phones not on BETA_PHONE_ALLOWLIST (Phase 999.28).

    Polite Danish holding message — students who stumble onto Lisa's pilot
    number during beta phase get a clear "not open yet, contact the school
    directly" response instead of silent drop (which looks broken) or full
    pipeline processing (which costs LLM tokens and pollutes the DB).
    """
    message = (
        "Tak for din besked. Lisa er endnu ikke åben for nye elever — "
        "kontakt skolen direkte for at booke en køretime."
    )
    return (
        f'<?xml version="1.0" encoding="UTF-8"?>'
        f'<Response><Message>{message}</Message></Response>'
    ).encode("utf-8")


def validate_twilio_request(request: Request, body: dict) -> bool:
    """Verify the request really came from Twilio.

    The local-development escape hatch is gated on an env var that is never set
    in production — deliberately NOT on the request hostname. ``request.url``
    is built from the client-supplied ``Host`` header, so a hostname check
    trusts exactly the thing an attacker controls: anything able to reach this
    process directly (Railway private networking, a misconfigured edge, a future
    sidecar) could send ``Host: localhost`` and skip signature validation
    entirely — injecting arbitrary "SMS" into the pipeline, which means LLM
    spend, fabricated bookings for allowlisted numbers, and owner-SMS spam.
    """
    if os.environ.get("LISA_SKIP_TWILIO_SIGNATURE") == "1":
        # Loud on purpose: if this ever appears in a production log, signature
        # validation is off and the webhook is accepting unauthenticated input.
        logger.warning(
            "LISA_SKIP_TWILIO_SIGNATURE=1 — Twilio signature validation BYPASSED. "
            "This must never be set in production."
        )
        return True

    url = str(request.url)
    # Behind reverse proxy, use X-Forwarded-Proto
    forwarded_proto = request.headers.get("x-forwarded-proto")
    if forwarded_proto:
        url = url.replace("http://", f"{forwarded_proto}://", 1)
    signature = request.headers.get("X-Twilio-Signature", "")
    return validator.validate(url, body, signature)


@router.post("/sms")
async def receive_sms(
    request: Request,
    From: str = Form(...),
    Body: str = Form(""),
    MessageSid: str = Form(""),
    db: Session = Depends(get_db),
):
    # Validate Twilio signature
    form_data = await request.form()
    params = {k: v for k, v in form_data.items()}
    if not validate_twilio_request(request, params):
        logger.warning("Invalid Twilio signature from %s", From)
        return Response(status_code=403)

    phone = From.strip()
    body = Body.strip()
    sid = MessageSid.strip() or None
    logger.info("Inbound SMS from %s: %s", phone, body[:50])

    # Beta-allowlist gate (Phase 999.28).
    # Empty BETA_PHONE_ALLOWLIST = open mode (current production behavior).
    # Populated = pilot mode: only OWNER_PHONE and listed numbers reach the
    # pipeline. Blocked phones get a Danish holding reply via TwiML and
    # return BEFORE conversation lookup, intent classification, LLM calls,
    # and any DB writes (zero cost, zero pollution for non-pilot phones).
    if BETA_PHONE_ALLOWLIST and phone != OWNER_PHONE and phone not in BETA_PHONE_ALLOWLIST:
        logger.info("Beta-gate: dropped SMS from non-allowlisted phone %s", phone)
        return Response(
            content=_beta_reject_twiml(),
            media_type="application/xml; charset=utf-8",
        )

    # Idempotency: Twilio retries the same MessageSid on any 5xx, network blip,
    # or timeout. Returning empty 200 TwiML stops further retries without
    # re-running side effects (booking creation, GCal writes, owner SMS).
    if sid:
        existing = db.query(Message).filter_by(message_sid=sid).first()
        if existing:
            logger.info("Duplicate MessageSid %s — short-circuiting", sid)
            return Response(content=EMPTY_TWIML, media_type="application/xml; charset=utf-8")

    # Get or create conversation
    conversation = db.query(Conversation).filter_by(phone_number=phone).first()
    if not conversation:
        conversation = Conversation(phone_number=phone)
        db.add(conversation)
        db.flush()

    # Log inbound message. If two requests with the same MessageSid arrive
    # near-simultaneously and both pass the dedup check above, the unique
    # index on message_sid will reject the second insert — treat that as
    # a duplicate too.
    message = Message(
        conversation_id=conversation.id,
        direction="inbound",
        body=body,
        timestamp=datetime.now(timezone.utc),
        message_sid=sid,
    )
    db.add(message)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        logger.info("Concurrent duplicate MessageSid %s — short-circuiting", sid)
        return Response(content=EMPTY_TWIML, media_type="application/xml; charset=utf-8")

    # Classify intent and generate response
    reply = handle_message(db, conversation, body)

    if reply:
        logger.info("Outbound reply to %s: %s", phone, reply[:160])
        # Log outbound message
        outbound = Message(
            conversation_id=conversation.id,
            direction="outbound",
            body=reply,
            timestamp=datetime.now(timezone.utc),
        )
        db.add(outbound)
        db.commit()

        # 999.33: deliver via the Twilio API, NOT by embedding in TwiML.
        # Intermediate flow replies (slot offers, type prompts, FAQ answers)
        # used to ride the webhook's TwiML response and were silently dropped
        # whenever the CalDAV/LLM work pushed the webhook past Twilio's ~15s
        # timeout — the student saw silence, then only the (already API-sent)
        # confirmation. API-sending every reply decouples delivery from HTTP
        # response timing and also removes the XML-escaping failure mode
        # (error 12200 on unescaped &, <, >). The two confirm handlers already
        # API-send and return "" so they don't double-send here.
        send_sms(phone, reply)
    else:
        logger.info("No reply to %s (rate-limited or intentionally silent)", phone)

    # Always ACK with empty TwiML — the reply (if any) was delivered via the API
    # above, so delivery no longer depends on this HTTP response arriving in time.
    return Response(content=EMPTY_TWIML, media_type="application/xml; charset=utf-8")
