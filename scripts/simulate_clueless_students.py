"""Pressure-test Lisa with 10 scripted "clueless student" conversations.

Drives Lisa via her live local webhook (`POST http://localhost:8000/sms`),
so this exercises the real classifier, the real LLM response generator, the
real booking flow, and yesterday's hot patches. CalDAV writes only fire if a
booking is confirmed — scenarios below intentionally STOP before confirmation
so no real calendar events are created.

Prereqs:
  1. uvicorn must be running locally, with signature validation bypassed —
     these requests carry no Twilio signature, so /sms 403s without it:
         LISA_SKIP_TWILIO_SIGNATURE=1 uvicorn app.main:app --reload --port 8000
     (The webhook used to trust anything claiming `Host: localhost`; that
     header comes from the client, so it authenticated nothing.)
  2. .env should have MAX_MESSAGES_PER_DAY=50 (default 3 will rate-limit some
     scenarios because each student receives multiple outbound replies).
  3. Local DB initialised:  python scripts/init_db.py

Run:
  python scripts/simulate_clueless_students.py
  python scripts/simulate_clueless_students.py --cleanup  # wipe test rows after
  python scripts/simulate_clueless_students.py --only 5   # run scenario 5 only

Test phones are +99900001..+99900010 (DK reserved test range) — never collides
with real numbers and easy to delete from the DB.
"""
import argparse
import re
import sys
import time
from dataclasses import dataclass, field
from typing import Callable

import requests

WEBHOOK_URL = "http://localhost:8000/sms"
MSG_RE = re.compile(r"<Message>(.*?)</Message>", re.DOTALL)

# Colors
G = "\033[92m"  # green
R = "\033[91m"  # red
Y = "\033[93m"  # yellow
B = "\033[1m"   # bold
C = "\033[96m"  # cyan
N = "\033[0m"   # reset


@dataclass
class Turn:
    """One inbound message + expectations about the reply."""
    msg: str
    must_contain: list[str] = field(default_factory=list)   # any one must appear (OR)
    must_not_contain: list[str] = field(default_factory=list)
    note: str = ""
    custom_check: Callable[[str], tuple[bool, str]] | None = None


@dataclass
class Scenario:
    id: int
    name: str
    pain_point: str
    phone: str
    turns: list[Turn]


# ---------------------------------------------------------------------------
# The 10 scenarios — each targets a specific real failure mode from trial day
# or a known weak spot. Stops before booking confirmation by design.
# ---------------------------------------------------------------------------
SCENARIOS: list[Scenario] = [
    Scenario(
        id=1,
        name="First-timer with vague opener",
        pain_point="Lead capture must engage; should not escalate on a 3-word hello",
        phone="+4599900001",
        turns=[
            Turn(
                msg="hej er du der",
                must_not_contain=["[LISA VIDERESTILLET]"],
                note="3-word opener — must not escalate. Should ask for name or context.",
            ),
        ],
    ),
    Scenario(
        id=2,
        name="Price FAQ — must NOT redirect to phone",
        pain_point="Yesterday's hot patch #2: FAQ generator was telling students to call the school",
        phone="+4599900002",
        turns=[
            Turn(
                msg="hvad koster en køretime?",  # intentional encoding glitch
                must_contain=["kr", "DKK", "koster", "Pris", "pris"],
                must_not_contain=["ring", "53 58 19 04", "kontakt"],
                note="FAQ must answer with price, NOT tell student to call.",
            ),
        ],
    ),
    Scenario(
        id=3,
        name="'How do I book' meta-question",
        pain_point="Phase 999.18 gap — should route to booking or tutorial, NOT escalate",
        phone="+4599900003",
        turns=[
            Turn(
                msg="hvordan booker jeg en køretime?",
                must_not_contain=["[LISA VIDERESTILLET]"],
                note="Currently classified as book_lesson — should enter booking flow.",
            ),
        ],
    ),
    Scenario(
        id=4,
        name="Booking with date range ('next week')",
        pain_point="Phase 999.5 — range vocabulary must yield slots inside next week",
        phone="+4599900004",
        turns=[
            Turn(
                msg="Jeg hedder Test Student",
                must_not_contain=["[LISA VIDERESTILLET]"],
            ),
            Turn(
                msg="kan jeg få en køretime i næste uge?",
                must_contain=["formiddag"],
                must_not_contain=["[LISA VIDERESTILLET]"],
                note="Post-2026-05-16 revert: fluffy time → expect time-of-day ask first.",
            ),
            Turn(
                msg="formiddag",
                must_contain=["kl", ":", "00", "15", "30", "45"],  # time pattern in reply
                must_not_contain=["[LISA VIDERESTILLET]"],
                note="Time-of-day chosen — must return actual slots within next week.",
            ),
        ],
    ),
    Scenario(
        id=5,
        name="Compositional time refinement ('efter skoletid')",
        pain_point="Yesterday's hot patch #2: slot_finder time-only fallback",
        phone="+4599900005",
        turns=[
            Turn(msg="Jeg hedder Maria Test"),
            Turn(
                msg="jeg vil gerne booke en køretime",
                must_contain=["formiddag"],
                note="Post-revert: fluffy booking → expect time-of-day ask first.",
            ),
            Turn(
                msg="formiddag",
                must_contain=["kl", ":"],
                note="Initial offer.",
            ),
            Turn(
                msg="kan jeg få en lidt senere tid? gerne efter skoletid",
                must_contain=["kl", ":"],
                must_not_contain=["[LISA VIDERESTILLET]"],
                note="Refinement must yield a later time, not the same slot or escalation.",
            ),
        ],
    ),
    Scenario(
        id=6,
        name="Pick-token trap: 'en' mid-sentence during offer",
        pain_point="Yesterday's hot patch #3 — 'en'/'anden' must not false-confirm",
        phone="+4599900006",
        turns=[
            Turn(msg="Jeg hedder Lars Test"),
            Turn(
                msg="jeg vil gerne booke en køretime",
                must_contain=["formiddag"],
                note="Post-revert: fluffy booking → expect time-of-day ask first.",
            ),
            Turn(
                msg="formiddag",
                must_contain=["kl", ":"],
                note="Get into BOOKING_OFFER_SLOT state.",
            ),
            Turn(
                msg="Hvordan booker jeg en køretime?",
                must_not_contain=["Booket!", "[LISA BOOKING]"],
                note="MUST NOT silently confirm slot 1 from the word 'en'.",
            ),
        ],
    ),
    Scenario(
        id=7,
        name="Pick-token trap: 'anden' mid-sentence during offer",
        pain_point="Yesterday's hot patch #3 sibling case",
        phone="+4599900007",
        turns=[
            Turn(msg="Jeg hedder Ida Test"),
            Turn(
                msg="jeg vil booke en køretime",
                must_contain=["formiddag"],
                note="Post-revert: fluffy → time-of-day ask first.",
            ),
            Turn(msg="formiddag", must_contain=["kl", ":"]),
            Turn(
                msg="Kan jeg få en anden tid?",
                must_not_contain=["Booket!", "[LISA BOOKING]"],
                note="MUST NOT silently confirm slot 2 from the word 'anden'.",
            ),
        ],
    ),
    Scenario(
        id=8,
        name="Mid-flow FAQ during offer (known gap — Phase 999.7)",
        pain_point="Phase 999.7 unfinished — currently re-prompts instead of answering",
        phone="+4599900008",
        turns=[
            Turn(msg="Jeg hedder Anders Test"),
            Turn(
                msg="jeg vil gerne booke en køretime",
                must_contain=["formiddag"],
                note="Post-revert: fluffy → time-of-day ask first.",
            ),
            Turn(msg="formiddag", must_contain=["kl", ":"]),
            Turn(
                msg="hvor mødes vi egentlig?",
                must_not_contain=["[LISA VIDERESTILLET]"],
                note="Expected to fail gracefully (re-prompt). Should NOT escalate. "
                     "Demo this gap if asked about future work.",
            ),
        ],
    ),
    Scenario(
        id=9,
        name="Number-collision: '2 spørgsmål' during offer",
        pain_point="Hardening — digit '2' inside a sentence must not pick slot 2",
        phone="+4599900009",
        turns=[
            Turn(msg="Jeg hedder Mette Test"),
            Turn(
                msg="jeg vil gerne booke en køretime",
                must_contain=["formiddag"],
                note="Post-revert: fluffy → time-of-day ask first.",
            ),
            Turn(msg="formiddag", must_contain=["kl", ":"]),
            Turn(
                msg="jeg har 2 spørgsmål først",
                must_not_contain=["Booket!", "[LISA BOOKING]"],
                note="Mid-sentence '2' should not be parsed as a slot pick.",
            ),
        ],
    ),
    Scenario(
        id=10,
        name="LLM hygiene — no emoji, no markdown",
        pain_point="Trial cosmetic issue — LLM still emits 😊 and *asterisks*",
        phone="+4599900010",
        turns=[
            Turn(
                msg="hvad er teori-skemaet?",
                custom_check=lambda r: (
                    not any(e in r for e in ("😊", "👍", "✅", "🚗", "🚙")) and "**" not in r,
                    "Reply contained emoji or markdown asterisks — fix system prompts.",
                ),
                note="Soft check: documents the cosmetic gap. Fail = patch the FAQ prompt.",
            ),
        ],
    ),
]


def send(phone: str, body: str) -> tuple[int, str]:
    """Send one inbound SMS to the local webhook. Returns (status, reply_text)."""
    try:
        r = requests.post(
            WEBHOOK_URL,
            data={"From": phone, "Body": body},
            timeout=30,
        )
    except requests.ConnectionError:
        print(f"{R}✗ Cannot reach {WEBHOOK_URL}. Is uvicorn running?{N}")
        sys.exit(1)
    except requests.Timeout:
        return 0, "<TIMEOUT — Lisa took >30s to respond>"
    m = MSG_RE.search(r.text)
    reply = m.group(1).strip() if m else "<EMPTY TwiML — rate-limited or silent>"
    return r.status_code, reply


def check_turn(reply: str, turn: Turn) -> tuple[bool, list[str]]:
    """Return (passed, list_of_failure_reasons)."""
    fails: list[str] = []

    if turn.must_contain:
        if not any(s.lower() in reply.lower() for s in turn.must_contain):
            fails.append(f"none of {turn.must_contain!r} found in reply")

    for s in turn.must_not_contain:
        if s.lower() in reply.lower():
            fails.append(f"forbidden substring {s!r} present in reply")

    if turn.custom_check:
        ok, msg = turn.custom_check(reply)
        if not ok:
            fails.append(msg)

    return (len(fails) == 0, fails)


def run_scenario(s: Scenario) -> tuple[int, int]:
    """Return (turns_passed, turns_total)."""
    print(f"\n{B}{C}═══ Scenario {s.id}: {s.name} ═══{N}")
    print(f"{C}Pain point:{N} {s.pain_point}")
    print(f"{C}Phone:{N}      {s.phone}")

    passed = 0
    for i, turn in enumerate(s.turns, 1):
        if turn.note:
            print(f"  {Y}↳ {turn.note}{N}")
        print(f"  {B}→ student:{N} {turn.msg}")
        status, reply = send(s.phone, turn.msg)
        # Trim reply to ~200 chars for readability
        display = reply if len(reply) <= 200 else reply[:200] + "…"
        print(f"  {B}← lisa:{N}    {display}")

        ok, fails = check_turn(reply, turn)
        if ok:
            print(f"  {G}✓ pass{N}")
            passed += 1
        else:
            for f in fails:
                print(f"  {R}✗ fail:{N} {f}")
        time.sleep(0.3)  # be kind to the LLM
    return passed, len(s.turns)


def cleanup(phones: list[str]) -> None:
    """Wipe test conversation/message rows. Imports the app, so run from repo root."""
    print(f"{Y}Cleaning up test rows for {len(phones)} phones…{N}")
    sys.path.insert(0, ".")
    from sqlalchemy.orm import sessionmaker
    from app.database import engine
    from app.models import Booking, Conversation, Lead, Message

    SessionLocal = sessionmaker(bind=engine)
    with SessionLocal() as db:
        for p in phones:
            conv = db.query(Conversation).filter_by(phone_number=p).first()
            if conv:
                db.query(Message).filter_by(conversation_id=conv.id).delete()
                db.delete(conv)
            db.query(Lead).filter_by(phone_number=p).delete()
            db.query(Booking).filter_by(phone_number=p).delete()
        db.commit()
    print(f"{G}✓ cleanup done{N}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--only", type=int, help="Run only scenario N (1-10)")
    ap.add_argument("--cleanup", action="store_true",
                    help="Delete test conversation/lead/booking rows after run")
    ap.add_argument("--cleanup-only", action="store_true",
                    help="Just cleanup, don't run scenarios")
    args = ap.parse_args()

    phones = [s.phone for s in SCENARIOS]

    if args.cleanup_only:
        cleanup(phones)
        return

    # Pre-cleanup so reruns are deterministic
    cleanup(phones)

    scenarios = [s for s in SCENARIOS if not args.only or s.id == args.only]
    print(f"{B}Running {len(scenarios)} scenarios against {WEBHOOK_URL}{N}")

    total_p = total_t = 0
    failed_scenarios: list[Scenario] = []
    for s in scenarios:
        p, t = run_scenario(s)
        total_p += p
        total_t += t
        if p < t:
            failed_scenarios.append(s)

    # Summary
    print(f"\n{B}═══ SUMMARY ═══{N}")
    print(f"Total turns: {total_p}/{total_t} passed")
    if failed_scenarios:
        print(f"\n{R}Scenarios with failures:{N}")
        for s in failed_scenarios:
            print(f"  - #{s.id} {s.name}  ({s.pain_point})")
    else:
        print(f"{G}All scenarios green ✓{N}")

    if args.cleanup:
        cleanup(phones)

    sys.exit(0 if total_p == total_t else 1)


if __name__ == "__main__":
    main()
