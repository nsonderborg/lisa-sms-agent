# CalDAV (iCloud) Setup for Lisa

Use this if your school wants to keep its bookings in Apple Calendar /
iCloud instead of Google Calendar. Lisa writes events directly into a
named calendar in your iCloud account via CalDAV.

> **Heads up.** Lisa needs *write* access to your calendar. Treat the
> credentials below as you would any production secret: keep them in
> Railway's environment variables, never commit them, and revoke the
> app-specific password if anything looks off.

## Prerequisites

- An Apple ID (the same one you use on iPhone / Mac).
- Two-factor authentication enabled on the Apple ID — this is required
  before Apple lets you generate app-specific passwords.

## Step 1 — Generate an app-specific password

1. Open <https://appleid.apple.com> and sign in.
2. Go to **Sign-In and Security** → **App-Specific Passwords** →
   **Generate an app-specific password**.
3. Name it something like `Lisa SMS Secretary` so you can revoke it
   later if needed.
4. Copy the 19-character password (format: `xxxx-xxxx-xxxx-xxxx`).
   You'll only see it once.

This password gives Lisa read/write access to your iCloud calendars.
Revoking it later (same page) immediately cuts Lisa off — handy as a
panic button.

## Step 2 — Create a dedicated calendar

Lisa won't write into your personal "Home" or "Work" calendar; it needs
its own. Open Apple Calendar (Mac or iPhone) and create a new calendar
named **Lisa** (or any name you prefer — note it for Step 3).

You can colour-code it however you like. All booking events land here.

## Step 3 — Configure Lisa

In `config.yaml`:

```yaml
calendar_backend: caldav
caldav_calendar_name: Lisa   # exactly the name you used in Step 2
```

In Railway (or your `.env` for local dev):

```
CALDAV_URL=https://caldav.icloud.com/
CALDAV_USERNAME=owner@example.com
CALDAV_PASSWORD=xxxx-xxxx-xxxx-xxxx
```

`CALDAV_URL` defaults to iCloud, so you can omit it if that's what
you're using.

> **Where the calendar name comes from (precedence matters).** Lisa
> resolves the calendar name in this order: the `CALDAV_CALENDAR_NAME`
> environment variable first, then `caldav_calendar_name` in
> `config.yaml`, then the built-in default `Lisa`. So **if
> `CALDAV_CALENDAR_NAME` is set, it wins** and silently overrides
> whatever is in `config.yaml`. Pick *one* place to set the name. If you
> ever set the env var (e.g. to a `Lisa-test` calendar while testing),
> remember to update or remove it before going live — a stale env var is
> the most common cause of a "calendar not found" failure that looks like
> the `config.yaml` value should have worked.

You can leave `gcal_calendar_id` and the `GOOGLE_*` env vars unset.
Lisa won't read them.

## Step 4 — Smoke test

Start the server and tail the logs. On the first calendar poll you
should see:

```
Cached N events from caldav calendar
```

Then make a test booking via SMS. Within seconds the event should
appear on your iPhone in the **Lisa** calendar.

## Troubleshooting

- **"CalDAV calendar named '...' not found"** — the resolved calendar
  name doesn't match a calendar in iCloud. Check the exact spelling in
  Apple Calendar (case matters). Note the name in the error message: if
  it isn't what you put in `config.yaml`, a `CALDAV_CALENDAR_NAME` env
  var is overriding it (see the precedence note in Step 3) — update or
  remove that env var.
- **`401 Unauthorized` in logs** — the app-specific password was
  revoked or your Apple ID password changed (which auto-revokes all
  app-specific passwords). Generate a new one and update the env var.
- **Events not showing up on iPhone** — iCloud sync can lag a few
  seconds. If it's been more than a minute, check that the **Lisa**
  calendar is enabled in **Settings → Calendar → Accounts → iCloud**.
- **Want to switch back to Google Calendar?** — change
  `calendar_backend: google` in `config.yaml`, set the `GOOGLE_*` env
  vars, and restart. The CalDAV credentials don't need to be removed.

## Why CalDAV?

The teacher who tested the prototype wanted to stay inside the Apple
ecosystem (no separate Google account). CalDAV is the open protocol
that lets external services write into iCloud calendars, the same way
Calendar.app on a Mac syncs with iCloud. It's the right tool for this
use case — but it's *also* not the only option Lisa supports. New
schools onboarding to Google Workspace can keep using
`calendar_backend: google` with no changes.
