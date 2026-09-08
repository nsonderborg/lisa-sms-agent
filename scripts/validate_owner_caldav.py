"""Validate owner-supplied CalDAV credentials BEFORE pushing them to Railway.

Connects to iCloud CalDAV with the given Apple ID + app-specific password,
lists all calendars on the account, and verifies the target calendar exists
by exact case-sensitive name match. Optionally round-trips a test event.

Usage:
    python3 scripts/validate_owner_caldav.py \
        --apple-id owner@example.com \
        --calendar-name Lisa-Demo
    # Prompts for app-specific password (hidden input).

    python3 scripts/validate_owner_caldav.py \
        --apple-id owner@example.com \
        --calendar-name Lisa-Demo \
        --roundtrip
    # Also creates and deletes a 15-min test event to verify write permission.
"""
import argparse
import getpass
import sys

import caldav

DEFAULT_URL = "https://caldav.icloud.com/"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apple-id", required=True, help="Apple ID e-mail")
    parser.add_argument(
        "--calendar-name", required=True, help="Expected calendar name (case-sensitive)"
    )
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument(
        "--password",
        help="App-specific password (omit to be prompted; recommended for shell-history safety)",
    )
    parser.add_argument(
        "--roundtrip",
        action="store_true",
        help="Also create and delete a 15-min test event to verify write permission",
    )
    args = parser.parse_args()

    password = args.password or getpass.getpass("App-specific password: ")

    print(f"Connecting to {args.url} as {args.apple_id}...")
    client = caldav.DAVClient(url=args.url, username=args.apple_id, password=password)
    try:
        principal = client.principal()
    except Exception as e:
        print(f"FAIL Auth failed: {e}", file=sys.stderr)
        return 1

    try:
        calendars = principal.calendars()
    except Exception as e:
        print(f"FAIL Could not list calendars: {e}", file=sys.stderr)
        return 1

    names = [str(c.get_display_name()).strip() for c in calendars]
    print(f"OK   Authenticated. Found {len(names)} calendar(s): {', '.join(names) or '<none>'}")

    target = args.calendar_name.strip()
    match = next(
        (c for c in calendars if str(c.get_display_name()).strip() == target), None
    )
    if not match:
        print(
            f"FAIL Calendar {target!r} NOT FOUND (case-sensitive). "
            f"Available: {', '.join(names)}",
            file=sys.stderr,
        )
        return 1
    print(f"OK   Calendar {target!r} found (exact match).")

    if args.roundtrip:
        import uuid
        from datetime import datetime, timedelta, timezone

        from icalendar import Calendar as ICal
        from icalendar import Event

        cal = ICal()
        cal.add("prodid", "-//Lisa CalDAV validator//")
        cal.add("version", "2.0")
        ev = Event()
        ev.add("uid", str(uuid.uuid4()))
        ev.add("summary", "Lisa CalDAV validation (auto-delete)")
        start = datetime.now(timezone.utc) + timedelta(hours=1)
        ev.add("dtstart", start)
        ev.add("dtend", start + timedelta(minutes=15))
        cal.add_component(ev)

        try:
            event = match.save_event(cal.to_ical().decode())
            print(f"OK   Created test event: {event.url}")
            event.delete()
            print("OK   Deleted test event.")
        except Exception as e:
            print(f"FAIL Round-trip failed: {e}", file=sys.stderr)
            return 1

    print("\nAll checks passed. Safe to push to Railway.\n")
    print("!! BEFORE YOU RUN THIS: `railway variables --set` REBUILDS PROD FROM `master`.")
    print("   Anything not merged to master will be silently dropped from production.")
    print("   Check `gh pr list` first and merge what prod needs.\n")
    print("Next step (copy-paste, replace ***):")
    print("  railway variables \\")
    print(f'    --set "CALDAV_USERNAME={args.apple_id}" \\')
    print('    --set "CALDAV_PASSWORD=***paste app-specific pw here***" \\')
    print(f'    --set "CALDAV_CALENDAR_NAME={target}"')
    print("  railway redeploy")
    print("\nThen VERIFY (a 200 from /health does not prove the calendar works):")
    print("  railway deployment list          # expect a NEW deployment")
    print('  railway logs | grep -E "Started server process|Cached .* busy intervals"')
    return 0


if __name__ == "__main__":
    sys.exit(main())
