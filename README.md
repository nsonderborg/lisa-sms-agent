# Lisa — an SMS booking agent for Danish driving schools
Link to a demo video here https://youtube.com/shorts/TqEK9m1pO6Q

A conversational agent that handles a driving school's inbound student messages end to end
over SMS: classifies intent, answers questions from the school's own config, offers real
free slots read from the owner's calendar, books, reschedules and cancels — and escalates to
a human when it should not act alone.

Built solo and deployed to production for one real driving school, not a demo.

```
Twilio SMS → POST /sms → conversation/manager.py
                              ↓
                     intent/classifier.py  (Claude, tool-use, 11 intents + confidence gate)
                              ↓
        ┌─────────────────────┼─────────────────────┐
        ↓                     ↓                     ↓
  response/generator     booking/handler      cancellation/handler
  (template · LLM · FAQ) (slot offers)        (policy check)
        └─────────────────────┼─────────────────────┘
                              ↓
                    calendar/client.py → Google Calendar | CalDAV (iCloud)
```

See [`lisa_system_architecture.svg`](lisa_system_architecture.svg) for the full picture.

---

## Status

It ran in production. Deployed on Railway against a real driving school owner's iCloud
calendar, taking live SMS on a real Danish number, with a beta allowlist, per-student rate
limiting, scheduled calendar polling and operational summaries to the owner. Bookings were
written to a real calendar and confirmed back over SMS.

---

## What's worth looking at

**[`docs/EVAL-SPEC-intent-classifier.md`](docs/EVAL-SPEC-intent-classifier.md)** — an
evaluation strategy for the highest-risk component: the classifier mapping untrusted Danish
student SMS to 11 intents behind a 0.7 confidence gate. It ranks failure modes by blast
radius (a `cancel_booking` ↔ `reschedule_booking` confusion writes to a real calendar; a
`greeting` ↔ `faq_question` confusion does not), and it scores **post-threshold behaviour** —
what the system actually acts on — rather than raw model output. Companion:
[`docs/intent_eval_seed.jsonl`](docs/intent_eval_seed.jsonl), 62 labeled cases with
context-sensitive pairs, since `"Ja"` means different things depending on the previous turn.

**Guardrails and fallback paths.** Confidence below 0.7 is rewritten to `unknown` *in code,
before routing* — the model does not get to decide whether it was confident enough. Unknown
twice escalates to a human with a `[LISA VIDERESTILLET]` prefix. Separately: max 3 automated
messages per student per day, a phone allowlist, whole-word urgency-keyword escalation that
bypasses intent entirely, and a cancellation policy that warns rather than hard-blocks.

**Test suite.** 36 files, ~9,100 lines of test code against ~7,800 lines of application code.
Includes `tests/test_pressure_scenarios.py` and `scripts/simulate_clueless_students.py` —
adversarial conversation scenarios rather than unit tests of happy paths.

**Two calendar backends behind one dispatcher** (`app/calendar/client.py`), Google Calendar
and CalDAV/iCloud, because the first real customer used Apple and the second did not.

**CI** runs ruff, pytest with coverage, and a non-blocking `pip-audit`, with `TZ` pinned to
Europe/Copenhagen, because the booking logic runs on Copenhagen wall-clock and CI runners
default to UTC.

---

## Running it

```bash
pip install -r requirements.txt -r requirements-dev.txt
cp .env.example .env          # fill in Twilio, Anthropic, calendar credentials
python scripts/init_db.py
uvicorn app.main:app --reload --port 8000
```

```bash
TZ=Europe/Copenhagen pytest        # 464 passed, 1 xfailed
ruff check .
```

Simulate an inbound SMS without Twilio:

```bash
LISA_SKIP_TWILIO_SIGNATURE=1 uvicorn app.main:app --reload --port 8000
python scripts/simulate_sms.py "Hej, har du en tid på torsdag?"
```

`config.yaml` holds the school-specific configuration — opening hours, appointment types and
durations, pricing, FAQ answers, cancellation deadlines. It is the whole onboarding surface
for a new school; the committed file is a fictional example.

---

## Stack

Python 3.11 · FastAPI · Twilio · Claude API (tool use) · SQLAlchemy on Postgres/SQLite ·
Google Calendar API · CalDAV · APScheduler · Streamlit · Railway

## Notes

Danish-first, with English detection per message. Responses target 160 characters and are
hard-capped at 320.

This is a sanitized public copy. The school owner's and pilot student's names, phone numbers
and calendar credentials have been removed and replaced with placeholders; the planning
history, which contains third-party personal data, is not included.
