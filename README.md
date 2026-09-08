# Lisa — an SMS booking agent for Danish driving schools

A conversational agent that handles a driving school's inbound student messages end to end
over SMS: classifies intent, answers questions from the school's own config, offers real
free slots read from the owner's calendar, books, reschedules and cancels — and escalates to
a human when it should not act alone.

Built solo over five months as a production system for one real driving school, not a demo.

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

## Status — read this first

**The system was built, deployed and ran live. It never got used by real students.**

It ran in production on Railway against a real school owner's iCloud calendar, with a
beta allowlist, rate limiting and operational summaries. Two students were invited by the
owner. Neither ever sent a message. The production database finished with zero student
conversations.

Then customer discovery killed the premise: across roughly ten driving instructors at eight
schools, the dominant reason for "no" was that instructors keep their calendar **on paper** —
and an agent that cannot read machine-readable busy/free time cannot function at all. Two of
them had tried a digital calendar and deliberately gone back.

That outcome is in this README on purpose. The engineering below is real and works; the
market thesis behind it did not survive contact with the customers. Both halves are the
point.

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
Europe/Copenhagen — see below for why that pin is load-bearing.

---

## Production lessons that cost something

These are the ones that changed how the system is built:

**A scheduled job that logs success while failing.** The calendar poller caught its own
exceptions and logged "executed successfully" regardless. `/health` returned 200 because it
only checked the web process. Twenty-two consecutive CalDAV authentication failures
accumulated behind a green status page, and the pilot was silently dead for weeks before
anyone looked at the right log line. A health check that does not assert on the thing that
actually breaks is decoration.

**A regression test that passes against the bug proves nothing.** Fixing a naive
`date.today()` in the slot finder, the first test passed against the *unfixed* code too —
patching `slot_finder.datetime` never touched `date.today()`, and the dev machine's real
date happened to sit on the harmless side of the boundary. Routine since: apply the fix,
write the test, revert the fix, **confirm red**, restore, confirm green. The corollary is
that CI structurally cannot catch this class, because CI pins `TZ` — a timezone test has to
fake the container's clock, never trust the runner's.

**Check the feature flag before debugging the feature.** Repeatedly, something "broken" was
simply not enabled. This codebase is full of silently-off switches by design; the diagnosis
step now starts at the gate, not the logic.

**A half-applied config change is worse than none.** Deleting a Railway variable removed it
from the dashboard but the running process had read env at import time, so it kept the old
value — the config looked correct and the behaviour was unchanged.

**Timezone frames must be stated, not assumed.** `Booking.start_dt` is naive Copenhagen
local; `CalendarCache` and `Message.timestamp` are UTC. Mixing them produced a bug class
that recurred until every boundary was labelled.

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
