# Lisa — Operations Runbook

## 1. Initial Setup (one-time per deployment)

### Prerequisites
- Python 3.11+
- Railway CLI (`brew install railway`)
- Cloudflare tunnel for local E2E (`brew install cloudflare/cloudflare/cloudflared`)
- A Twilio account with a phone number
- A Google Cloud project with the Calendar API enabled and an OAuth2 client (type: Desktop)
- An Anthropic API key

### Steps

```bash
git clone <repo>
cd Lisa-driving-school-secretary
pip install -r requirements.txt
cp .env.example .env         # fill in all values (see below)
python scripts/init_db.py    # create local SQLite database
```

### Google Calendar auth (OAuth2)
```bash
# Add to .env first:
# GOOGLE_SERVICE_ACCOUNT_OAUTH2_CLIENT_ID=...
# GOOGLE_SERVICE_ACCOUNT_OAUTH2_CLIENT_SECRET=...

PYTHONPATH=. python3 scripts/auth_gcal.py
# → Opens browser, complete the OAuth consent flow
# → token.json is written locally

# Extract the refresh_token from token.json and add to .env:
# GOOGLE_REFRESH_TOKEN=...
# GOOGLE_CLIENT_ID=...       (same as OAUTH2_CLIENT_ID)
# GOOGLE_CLIENT_SECRET=...   (same as OAUTH2_CLIENT_SECRET)
```

### config.yaml
Fill in school-specific info: `school_name`, `address`, `gcal_calendar_id`, `pricing`, `theory_schedule`, `appointment_types`, `faq`.

**One deploy serves one school.** The file is chosen by `CONFIG_PATH`, defaulting to
`config.yaml` at the repo root. A relative value resolves against the repo root (not the
process working directory, which differs on Railway); an absolute value is used as-is.

```bash
CONFIG_PATH=config.oemer.yaml uvicorn app.main:app --reload --port 8000
```

Onboarding a second school is therefore a **second Railway service** with its own
`CONFIG_PATH`, not a shared deploy. Set per service, or students of one school reach the
other school's owner: `OWNER_PHONE`, `SCHOOL_OWNER_PHONE`, `BETA_PHONE_ALLOWLIST`,
`TWILIO_PHONE_NUMBER`, `DATABASE_URL`, and the calendar credentials.

⚠️ `config.py` reads env at **import time**, so a `CONFIG_PATH` change only takes effect on
process start — after any `railway variables --set`, confirm `railway deployment list` shows a
NEW deployment, else `railway redeploy -y`. A half-applied config change looks correct while
behaviour is unchanged.

⚠️ `CALENDAR_BACKEND` set as an env var **overrides** `calendar_backend` in the config file.
It is currently set on the production service, so a new service must set it explicitly.

---

## 2. Local Development

```bash
# Start the server
PYTHONPATH=. uvicorn app.main:app --reload --port 8000

# Simulate an inbound SMS (no real Twilio cost)
PYTHONPATH=. python3 scripts/simulate_sms.py "Jeg vil gerne booke en køretime"

# Reset a test conversation (clears state + message history)
PYTHONPATH=. python3 scripts/reset_user.py           # defaults to +4500000002
PYTHONPATH=. python3 scripts/reset_user.py +45XXXXXXXX

# Verify GCal connection
PYTHONPATH=. python3 scripts/test_gcal.py
```

---

## 3. Local E2E with Real SMS

Use this when you want to test with a real phone and real Twilio, but against your local server.

```bash
# Terminal 1 — start the app
PYTHONPATH=. uvicorn app.main:app --port 8000

# Terminal 2 — expose it publicly
cloudflared tunnel --url http://localhost:8000
# → prints a URL like https://abc-xyz.trycloudflare.com

# In Twilio Console → your number → Messaging → webhook:
# https://abc-xyz.trycloudflare.com/sms  (HTTP POST)

# Text your Twilio number from your phone, watch Terminal 1 logs.

# When done, switch the Twilio webhook back to the Railway URL.
```

---

## 4. Deploy to Railway

```bash
# Set env vars (only needed when adding/changing vars)
railway variable set KEY=value --service lisa-driving-school

# Deploy current directory
railway up --detach -m "description of change" --service lisa-driving-school

# Watch logs
railway logs --service lisa-driving-school --lines 50
```

### Required Railway env vars
```
TWILIO_ACCOUNT_SID
TWILIO_AUTH_TOKEN
TWILIO_PHONE_NUMBER
ANTHROPIC_API_KEY
OWNER_PHONE
DATABASE_URL          # injected automatically by Railway Postgres
```

Plus the calendar credentials matching `calendar_backend` in `config.yaml`:

```
# When calendar_backend == google:
GOOGLE_CLIENT_ID
GOOGLE_CLIENT_SECRET
GOOGLE_REFRESH_TOKEN

# When calendar_backend == caldav:
CALDAV_USERNAME       # Apple ID e-mail
CALDAV_PASSWORD       # app-specific password from appleid.apple.com
CALDAV_URL            # optional, defaults to https://caldav.icloud.com/
```

For the iCloud setup walkthrough (one-time per school) see `docs/CALDAV_SETUP.md`.

### Twilio webhook (production)
```
https://lisa-driving-school-production.up.railway.app/sms
```

---

## 5. Day-to-Day Operations

### Check logs
```bash
railway logs --service lisa-driving-school --lines 100
```

### Reset a student conversation

**Locally** (works against both SQLite and Railway Postgres via DATABASE_URL):
```bash
PYTHONPATH=. python3 scripts/reset_user.py +45XXXXXXXX
```

**Remotely on Railway** via the admin HTTP endpoint (no database access needed):
```
GET https://lisa-driving-school-production.up.railway.app/admin/reset?phone=+45XXXXXXXX&token=YOUR_ADMIN_TOKEN
```
Set `ADMIN_TOKEN` as a Railway env var to enable this endpoint. Deletes the conversation, all messages, and all bookings for that phone number.

### GCal token expired / revoked
OAuth2 refresh tokens can be revoked if the GCP app is in test mode and the token is >7 days old, or if the user revokes access.

```bash
PYTHONPATH=. python3 scripts/auth_gcal.py   # re-authenticate, generates new token.json
# Extract new refresh_token from token.json
railway variable set GOOGLE_REFRESH_TOKEN=<new_token> --service lisa-driving-school
railway up --detach -m "fix: refresh GCal token" --service lisa-driving-school
```

### Update school config (pricing, FAQ, schedule)
Edit `config.yaml` locally, then redeploy:
```bash
railway up --detach -m "config: update pricing/FAQ" --service lisa-driving-school
```

---

## 6. Architecture Overview

See `CLAUDE.md` for full system architecture. Quick reference:

- **Inbound SMS** → Twilio → `POST /sms` → `webhook.py`
- **Intent classification** → Claude API (`claude-haiku-4-5`)
- **Booking/cancellation/FAQ** → handlers → Google Calendar API
- **Outbound SMS** → Twilio API via `sender.py`
- **Background jobs** → APScheduler (reminders, GCal polling, daily summaries)
- **Database** → PostgreSQL on Railway (SQLAlchemy ORM)
- **Config** → `config.yaml` (school info) + `.env` (secrets)

### Calendar cache behaviour

Events are fetched from Google Calendar and stored as naive UTC in the `CalendarCache` table. The scheduled background poll runs 3×/day (08:00, 13:00, 17:00 Copenhagen time, set via `calendar_poll_hours` in `config.yaml`).

In addition, every booking search (`find_earliest_slot` / `find_slots_for_preferences`) calls `refresh_if_stale()` before querying the cache. If the cache is older than 15 minutes it triggers an immediate poll before computing slots. This caps the double-booking risk window at 15 minutes without making a GCal API call on every SMS. If the GCal API is unreachable the function falls back silently to the existing cache.

After a booking is confirmed or cancelled, `poll_calendar()` is called immediately to reflect the change in the cache. This means a newly booked slot is blocked for other students within seconds, and a cancelled slot becomes bookable again without waiting for the next scheduled poll.

Slot math runs entirely in Copenhagen local time. `get_busy_intervals()` converts cached UTC events to local time before returning them, so the busy intervals and the work window (from `work_start`/`work_end` in `config.yaml`) are always in the same timezone.

### Slot preference resolution

Students can state preferences at three levels of specificity, all understood:

1. **Day name** — "mandag", "tirsdag" etc. → search only on those weekdays
2. **Time-of-day window** — "morgen" (07–09), "formiddag" (09–12), "eftermiddag" (12–16), "aften" (16–18) → filter slots to that window
3. **Specific clock time** — "kl 14", "halv tre", "klokken 9", "2PM" etc. → captured as `preferred_time` (HH:MM) and used to filter slots to those at or after that time within the window

All three can be combined: "mandag kl 14" → Monday, eftermiddag window, slots ≥ 14:00 → first offer is 14:15. If no slots are available at or after the preferred time, the system falls back to the earliest slot in the window rather than failing.

`preferred_time` is extracted by the Claude Haiku preference extractor (`app/booking/preference_extractor.py`) and threaded through the full booking and reschedule flows.

### Booking safeguards

The following protections are in place against double-booking and silent failures (all added 2026-05-01):

- **Availability re-check at confirmation** — when a student says "ja" to a slot offer, the Booking table is queried for overlapping confirmed bookings before writing. If another student claimed the slot in the meantime, the student is told it was just taken and returned to IDLE.
- **GCal write failure → owner alert** — if writing to Google Calendar fails during a booking or reschedule, the owner receives a `[LISA FEJL]` SMS with the student's number and time, so the instructor can add the entry manually. The student's booking is still confirmed in the database.
- **Reschedule atomicity** — during a reschedule, the new GCal event is created first. The old event is only deleted after the new one is confirmed. If the create fails, the old event is preserved and the owner is alerted.

### Conversation timeouts and mid-flow resets

The no-reply timeout job runs every 30 minutes and handles two scenarios:

- **`awaiting_confirmation`** (reminder flow) — if no reply after `no_reply_timeout_hours` (default: 4h), state resets to IDLE and the owner is alerted.
- **All mid-flow states** (booking, cancellation, lead intake, clarification) — reset silently to IDLE after 4 hours. No owner alert; the student can simply start over.
- **Reschedule states specifically** — at 1 hour of inactivity, Lisa sends a nudge: *"Du var ved at omplanlægge din tid. Vil du fortsætte?"*. If still no reply at 4 hours, the flow resets and the `reschedule_pending` booking is automatically reverted to `confirmed` so the student's original slot is preserved.

### Rate limiting

The 3-message-per-day automated limit applies **only to proactive outbound messages** (reminders, summaries). Student-initiated messages always receive a response regardless of how many automated messages were sent that day. This prevents the rate limit from blocking a student who is actively trying to book after receiving morning/afternoon reminders.

---

## 7. Troubleshooting

### Slots offered that are already blocked in Google Calendar

**Symptom:** Lisa offers a time slot that has an existing event on the calendar.

**Likely causes and checks:**

1. **Cache was stale at booking time** — the on-demand `refresh_if_stale()` call (15-min threshold) should prevent this in normal operation. Check Railway logs for lines like:
   ```
   Calendar cache is XX min old — refreshing
   ```
   If the GCal API was unreachable at the time, the refresh silently falls back to the stale cache. Verify GCal connectivity (see *GCal token expired* in section 5).

2. **Event added to calendar between polls and no booking triggered the refresh** — wait up to 15 minutes or manually trigger a poll:
   ```bash
   PYTHONPATH=. python3 -c "from app.calendar.poller import poll_calendar; print(poll_calendar())"
   ```

3. **Timezone mismatch (historical, should not recur)** — prior to the 2026-05-01 fix, busy intervals were compared against work hours using mixed timezones (cached events as naive UTC, work window as naive Copenhagen local), which caused a 2-hour offset in summer (CEST). This is resolved: `get_busy_intervals()` now converts all intervals to Copenhagen local before returning them.

### Owner receives `[LISA FEJL]` alert

**Symptom:** SMS to owner saying "Kalender IKKE opdateret for…"

This means a booking or reschedule was confirmed in the database but the Google Calendar write failed. The student has been confirmed. Action required:

1. Manually add the appointment to Google Calendar at the time shown in the alert.
2. Check Railway logs for the underlying error (usually an expired token or network issue).
3. If token-related, follow the *GCal token expired* steps in section 5.

### Student stuck mid-booking or mid-reschedule

Mid-flow states (booking, cancellation, reschedule) time out automatically after 4 hours and reset to IDLE. If a student needs an immediate reset:
```bash
PYTHONPATH=. python3 scripts/reset_user.py +45XXXXXXXX
```
For reschedule flows specifically, the `reschedule_pending` booking status is reverted to `confirmed` automatically on reset, so the student's original slot is not lost.

### GCal poll not running / logs show 0 events cached

- Confirm `gcal_calendar_id` is set correctly in `config.yaml`.
- Confirm `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_REFRESH_TOKEN` are set in Railway.
- Run the GCal test script locally: `PYTHONPATH=. python3 scripts/test_gcal.py`
- If the token is expired, follow the *GCal token expired* steps in section 5.
