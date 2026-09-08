"""Preferred-time handling + acknowledgment (2026-06-05).

Two behaviors:
1. filter_slots_by_preferred_time tolerates a slot up to 15 min BEFORE the
   requested time (so 11:45 counts for a 12:00 request when 12:00 sharp
   collides with the inter-lesson buffer).
2. When no slot is within ±15 min of the requested time, the offer layer
   acknowledges it ("Kl. 12 er ikke ledig, men ledige tider er …") instead of
   silently offering unrelated times.
"""
from datetime import datetime

from app.booking.slot_finder import (
    filter_slots_by_preferred_time,
    preferred_time_satisfied,
)
from app.booking.handler import _emit_time_unavailable_offer, _format_pref_time


def _slots(*hhmm):
    return [datetime(2026, 6, 6, h, m) for h, m in hhmm]


# --- preferred_time_satisfied (±15 min) ------------------------------------

def test_satisfied_when_slot_within_15_min_before():
    assert preferred_time_satisfied(_slots((11, 45)), "12:00") is True


def test_satisfied_when_slot_within_15_min_after():
    assert preferred_time_satisfied(_slots((12, 15)), "12:00") is True


def test_not_satisfied_when_nearest_is_45_min_off():
    # The real bug: "kl 12" but only 09:15/10:15/11:15 available.
    assert preferred_time_satisfied(_slots((9, 15), (10, 15), (11, 15)), "12:00") is False


def test_unparseable_preferred_time_is_treated_as_satisfied():
    assert preferred_time_satisfied(_slots((9, 0)), "garbage") is True


# --- filter tolerance ------------------------------------------------------

def test_filter_includes_slot_15_min_before():
    slots = _slots((11, 45), (12, 30))
    assert _slots((11, 45))[0] in filter_slots_by_preferred_time(slots, "12:00")


def test_filter_excludes_slot_more_than_15_before():
    # 11:15 is 45 min before 12:00 → excluded; soft-fallback returns original
    # only when NOTHING qualifies, so here 12:30 qualifies and 11:15 drops out.
    slots = _slots((11, 15), (12, 30))
    out = filter_slots_by_preferred_time(slots, "12:00")
    assert _slots((11, 15))[0] not in out
    assert _slots((12, 30))[0] in out


# --- acknowledgment copy ---------------------------------------------------

def test_pref_time_formatting():
    assert _format_pref_time("12:00") == "kl. 12"
    assert _format_pref_time("12:30") == "kl. 12:30"


def test_unavailable_offer_acknowledges_and_lists_alternatives():
    slots = _slots((9, 15), (10, 15), (11, 15))
    reply, _state = _emit_time_unavailable_offer("da", "12:00", slots)
    assert "ikke ledig" in reply.lower()
    assert "kl.09:15" in reply  # alternatives listed
    assert "11:15" in reply


def test_unavailable_offer_single_slot_phrasing():
    reply, _state = _emit_time_unavailable_offer("da", "12:00", _slots((14, 0)))
    assert "ikke ledig" in reply.lower()
    assert "Nærmeste" in reply
