"""Mid-flow FAQ pass-through helper (shared by booking, cancel, and reschedule flows).

Problem
-------
Today, a student inside BOOKING_OFFER_SLOT / CANCEL_CONFIRM /
RESCHEDULE_SELECT_SLOT / RESCHEDULE_CONFIRM who asks "hvor mødes vi?"
or "hvad koster det?" receives a "Svar 'ja' for at bekræfte"-style
reprompt — the question dies. This module re-introduces classification
at the mid-flow entry point and routes FAQ-shaped messages to the
existing IDLE-state generators (mirroring app/conversation/manager.py
:_route_by_intent) before re-stating the pending offer.

Placement rationale
-------------------
This is a CONVERSATION-level concern shared across three flow handlers
(booking, cancellation, reschedule). It lives next to manager.py for
the same reason the IDLE-state dispatcher does: it owns the
classify→route→generate pipeline, just at a different state entry
point.

Why classify again
------------------
manager.py only classifies messages received while the conversation is
in IDLE state. Once a flow handler takes over (BOOKING_OFFER_SLOT,
CANCEL_CONFIRM, etc.) the message is dispatched directly to that
handler WITHOUT classification — the handler assumes a confirmation
("ja"), a slot pick ("2"), or noise. This helper restores
classification for the FAQ-shaped subset.

Defense-in-depth confidence floor
---------------------------------
classify_message already downgrades anything below 0.7 to
"unknown" (see app/intent/classifier.py). We re-check confidence here
to harden against future drift in CONFIDENCE_THRESHOLD — if that
constant is ever lowered globally, mid-flow pass-through still
requires a strict 0.7.

Cap-at-2 semantics
------------------
Per ROADMAP, mid-flow FAQ pass-throughs are capped at 2 per offer
attempt; the third unrelated message escalates. The cap lives in the
CALLER (booking/cancellation handlers know what "escalation" means in
their flow). This module only provides count plumbing
(increment_faq_count / get_faq_count / reset_faq_count) and exports
MAX_MID_FLOW_FAQ_PASSTHROUGHS so callers share one source of truth.
"""

from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from app.config import CONFIDENCE_THRESHOLD as _CONFIG_THRESHOLD
from app.intent.classifier import classify_message
from app.response.generator import generate_faq_response, generate_response

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Module-level contract constants (Plan 02/03 import these by name)
# ---------------------------------------------------------------------------

MID_FLOW_FAQ_INTENTS: frozenset[str] = frozenset(
    {"location_question", "theory_question", "faq_question"}
)
"""Intents the mid-flow helper will answer through and re-state the offer."""

CONFIDENCE_THRESHOLD: float = 0.7
"""Local floor for routing a message through the mid-flow FAQ path.

Mirrors app.config.CONFIDENCE_THRESHOLD; module-level assertion below
fails fast if the two ever drift apart.
"""

# Fail-fast guard against silent drift between this module and config.
assert CONFIDENCE_THRESHOLD == _CONFIG_THRESHOLD, (
    "CONFIDENCE_THRESHOLD drift: "
    f"mid_flow_faq={CONFIDENCE_THRESHOLD} vs config={_CONFIG_THRESHOLD}"
)

MAX_MID_FLOW_FAQ_PASSTHROUGHS: int = 2
"""Cap on consecutive mid-flow FAQ pass-throughs per offer attempt.

Per ROADMAP: the third unrelated message in a row escalates as today.
The cap is enforced by callers (booking/cancellation handlers); this
module just exposes the constant and the counter plumbing.
"""


COPENHAGEN_TZ = ZoneInfo("Europe/Copenhagen")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def try_mid_flow_faq(
    body: str,
    language: str,
    conversation_context: str,
    offered_slot_iso: str | None,
) -> str | None:
    """Attempt to answer ``body`` as a mid-flow FAQ and re-state the offer.

    Returns the combined "FAQ answer + re-state-offer phrase" string on
    success, or ``None`` if the message is not an FAQ-shaped question at
    confidence >= 0.7. Returning ``None`` is the caller's signal to fall
    through to its existing handler logic (yes/no parser, slot picker,
    etc.).

    Parameters
    ----------
    body:
        The raw inbound SMS body to classify and (maybe) answer.
    language:
        The current conversation language ("da" or "en"). Comes from the
        caller's flow context — it is the language the offer was made in,
        not whatever the classifier reports for this single message.
    conversation_context:
        Pre-formatted recent-history string (caller already builds this
        for the classifier and the generator; passed through verbatim).
    offered_slot_iso:
        ISO-format datetime string of the current offer (e.g.
        "2026-05-20T10:00:00"). If ``None``, the FAQ answer is returned
        alone and the caller is responsible for appending its own
        re-prompt phrase.
    """
    result = classify_message(body, conversation_context)

    if result.intent not in MID_FLOW_FAQ_INTENTS:
        return None

    if result.confidence < CONFIDENCE_THRESHOLD:
        # Defense-in-depth — classifier already downgrades below 0.7,
        # but a future config change must not silently lower this floor.
        logger.info(
            "mid-flow FAQ: confidence %.2f below threshold for intent %s; not routing",
            result.confidence,
            result.intent,
        )
        return None

    if result.intent == "faq_question":
        answer = generate_faq_response(language, body, conversation_context)
    else:
        # location_question / theory_question — template-driven in
        # app.response.templates, no LLM call.
        answer = generate_response(result.intent, language, body, conversation_context)

    if offered_slot_iso is None:
        return answer

    try:
        slot_dt = datetime.fromisoformat(offered_slot_iso)
    except ValueError:
        logger.warning(
            "mid-flow FAQ: could not parse offered_slot_iso=%r; returning answer alone",
            offered_slot_iso,
        )
        return answer

    formatted = _format_slot(slot_dt)
    return answer + format_restate_offer_phrase(language, formatted)


def format_restate_offer_phrase(language: str, formatted_slot: str) -> str:
    """Return the re-state-offer suffix (leading space included).

    Caller concatenates this onto the FAQ answer. Phrasing is locked by
    Phase 999.7's ROADMAP:
        DA: " Stadig i forslag: {formatted_slot} — passer det?"
        EN: " Still on offer: {formatted_slot} — does it work?"
    """
    if language == "en":
        return f" Still on offer: {formatted_slot} — does it work?"
    return f" Stadig i forslag: {formatted_slot} — passer det?"


# ---------------------------------------------------------------------------
# Counter plumbing (cap enforcement lives in the caller)
# ---------------------------------------------------------------------------

def increment_faq_count(context: dict, flow_key: str) -> int:
    """Increment ``context[flow_key]["mid_flow_faq_count"]`` and return the new value.

    Creates ``context[flow_key]`` if absent. The counter is an integer
    that starts at 0.
    """
    bucket = context.setdefault(flow_key, {})
    new_value = int(bucket.get("mid_flow_faq_count", 0)) + 1
    bucket["mid_flow_faq_count"] = new_value
    return new_value


def reset_faq_count(context: dict, flow_key: str) -> None:
    """Reset the mid-flow FAQ counter for ``flow_key`` to 0.

    No-op if ``context[flow_key]`` doesn't exist yet.
    """
    bucket = context.get(flow_key)
    if isinstance(bucket, dict):
        bucket["mid_flow_faq_count"] = 0


def get_faq_count(context: dict, flow_key: str) -> int:
    """Return the current mid-flow FAQ counter for ``flow_key`` (0 if absent)."""
    bucket = context.get(flow_key)
    if not isinstance(bucket, dict):
        return 0
    return int(bucket.get("mid_flow_faq_count", 0))


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _format_slot(dt: datetime) -> str:
    """Format a naive Copenhagen-local datetime as a compact Danish-friendly string.

    Mirrors the implementation duplicated in app/booking/handler.py and
    app/cancellation/handler.py verbatim (output: 'tir 15/4 kl.10:00').
    Kept in sync deliberately — Plan 02 may consolidate later.
    """
    local = dt.replace(tzinfo=COPENHAGEN_TZ)
    day_abbrev = ["man", "tir", "ons", "tor", "fre", "lør", "søn"][local.weekday()]
    return f"{day_abbrev} {local.day}/{local.month} kl.{local.strftime('%H:%M')}"
