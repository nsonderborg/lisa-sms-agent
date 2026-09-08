"""Natural-language affirmation detection (Phase 999.1).

The booking and cancellation flows used to confirm only when the *entire*
message was a single word in a small set ({ja, ok, yes, ...}). Real students
reply "ja tak", "det lyder godt", "perfekt", "👍" — all of which fell through to
a re-prompt or a false escalation mid-flow.

`is_affirmation` accepts a message when every word token is affirmative (with
emoji support) and nothing carries a competing signal — a number, a weekday, a
time, a negation, or any other content word blocks it, so slot-picks ("ja, 2")
and preference changes ("ja men tirsdag") still route to their own handlers
instead of being swallowed as a blanket confirmation. This deliberately errs on
the side of NOT confirming when in doubt.
"""

# Affirmative word tokens (lowercased, punctuation/emoji stripped).
_AFFIRM_WORDS = {
    # Danish
    "ja", "jo", "jep", "jeps", "jada", "jotak", "ok", "okay", "okey", "tak",
    "gerne", "perfekt", "fint", "fin", "godt", "super", "lyder", "passer",
    "det", "er", "den", "selvfølgelig", "klart", "sagtens", "skønt", "dejligt",
    # English
    "yes", "yeah", "yep", "yup", "y", "sure", "please", "sounds", "good",
    "great", "perfect", "fine", "works", "ok", "okay",
}

# Emoji that read as agreement on their own.
_AFFIRM_EMOJI = {"👍", "👌", "🙏", "✅", "😊", "🙂", "💪", "👏", "🤙"}

# Negations that must veto an otherwise-affirmative message ("nej tak").
_REJECT_WORDS = {"nej", "no", "cancel", "annuller", "stop", "niks", "næ", "ellers"}


def is_affirmation(body: str) -> bool:
    """True when `body` is a plain yes with no competing intent.

    Examples that pass: "ja", "ja tak", "ja det passer", "lyder godt",
    "perfekt", "yes please", "ok 👍", "👍".
    Examples that fail: "ja, 2", "ja men tirsdag", "nej tak", "kan jeg få en
    anden tid?", "perfekt på mandag".
    """
    s = (body or "").strip().lower()
    if not s:
        return False

    has_affirm_emoji = any(ch in _AFFIRM_EMOJI for ch in s)

    # Replace any non-alphanumeric, non-space char (punctuation AND emoji) with a
    # space, then tokenize. Emoji removal is intentional — agreement emoji are
    # scored separately via has_affirm_emoji.
    cleaned = "".join(ch if (ch.isalnum() or ch.isspace()) else " " for ch in s)
    tokens = cleaned.split()

    # A negation anywhere vetoes (e.g. "nej tak", "ellers nej").
    if any(t in _REJECT_WORDS for t in tokens):
        return False

    if not tokens:
        # Emoji-only (or punctuation-only) message — confirm iff an agreement
        # emoji is present.
        return has_affirm_emoji

    # Every word token must be affirmative. A single foreign content word
    # (a number, weekday, "tid", "anden", a question word) blocks the confirm.
    return all(t in _AFFIRM_WORDS for t in tokens)
