import re
from enum import StrEnum


class ConversationState(StrEnum):
    IDLE = "idle"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    AWAITING_CLARIFICATION = "awaiting_clarification"
    LEAD_INTAKE_NAME = "lead_intake_name"
    LEAD_INTAKE_LICENSE = "lead_intake_license"
    LEAD_INTAKE_DATE = "lead_intake_date"
    ESCALATED = "escalated"
    BOOKING_INTAKE_NAME = "booking_intake_name"
    BOOKING_INTAKE_LESSON = "booking_intake_lesson"    # 999.37: capture lektionsplan position
    BOOKING_CONFIRM_LESSON = "booking_confirm_lesson"  # 999.37: nudge — verify the content label
    BOOKING_SELECT_TYPE = "booking_select_type"
    BOOKING_OFFER_SLOT = "booking_offer_slot"
    BOOKING_SELECT_DAY = "booking_select_day"
    BOOKING_SELECT_TIME = "booking_select_time"
    CANCEL_CONFIRM = "cancel_confirm"
    RESCHEDULE_SELECT_SLOT = "reschedule_select_slot"
    RESCHEDULE_CONFIRM = "reschedule_confirm"


# Intents that always escalate to the owner
# cancel_booking and reschedule_booking are now handled by the cancellation flow
ALWAYS_ESCALATE_INTENTS: set[str] = set()

# Urgency terms that trigger immediate escalation to the owner.
#
# Matched as WHOLE WORDS (see contains_urgency_keyword), not substrings — the
# old substring check falsely escalated everyday words that merely *contain* a
# keyword: "uheldigvis" (unfortunately) ⊃ "uheld", "ulykkelig" (unhappy) ⊃
# "ulykke", "politik" (politics) ⊃ "politi". Because Danish inflects nouns with
# suffixes, the list includes the real inflected forms (politi → politiet,
# ulykke → ulykken) so genuine reports like "politiet har taget mit kørekort"
# still escalate.
#
# Pruned to terms that are both realistic for a driving school AND serious
# enough to warrant a human. Dropped from the old list: generic "nødsituation"
# kept (harmless, real word); English "crash" (→ "crash course"), "accident"
# (→ "by accident"), "urgent" (too generic) removed as false-positive prone.
# This list is a deliberate policy choice — revisit with the owner.
URGENCY_KEYWORDS_DA = {
    "ulykke", "ulykken", "ulykker",
    "uheld", "uheldet",
    "politi", "politiet",
    "akut",
    "nødsituation",
}
URGENCY_KEYWORDS_EN = {"emergency", "police"}
URGENCY_KEYWORDS = URGENCY_KEYWORDS_DA | URGENCY_KEYWORDS_EN

_URGENCY_WORD_RE = re.compile(r"\w+", re.UNICODE)


def contains_urgency_keyword(text: str) -> bool:
    """True if `text` contains an urgency term as a WHOLE WORD.

    Tokenizes on word boundaries (Unicode \\w, so æøå count as letters) and
    checks set membership — so "uheldigvis", "ulykkelig", "politik" do NOT match,
    while "uheld", "ulykke", "politiet" do.
    """
    words = set(_URGENCY_WORD_RE.findall(text.lower()))
    return bool(words & URGENCY_KEYWORDS)
