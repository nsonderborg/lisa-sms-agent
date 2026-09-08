"""READ-ONLY live smoke-test for the privacy-preserving multi-calendar busy-read.

De-risks the one open question behind Phase 999.30: does iCloud
``principal.calendars()`` actually enumerate ALL of an account's calendars, and
does ``.search()`` succeed per calendar over a future window? This mirrors the
connect + enumerate structure of ``validate_owner_caldav.py`` but is STRICTLY
READ-ONLY — it never creates, never writes, never removes anything.

It also never prints event SUMMARY / LOCATION / DESCRIPTION — only per-calendar
event COUNTS — matching the no-content-in-logs rule the production code follows.

⚠️  READ-ONLY. Run this against YOUR OWN (Nikolas's) iCloud account first.
    NEVER run against the owner's production account without explicit consent.
    This script never writes.

Usage:
    python3 scripts/smoke_freebusy_all_calendars.py \
        --apple-id you@example.com
    # Prompts for app-specific password (hidden input).

    python3 scripts/smoke_freebusy_all_calendars.py \
        --apple-id you@example.com --days-ahead 14
"""
import argparse
import getpass
import sys
from datetime import datetime, timedelta, timezone

import caldav

DEFAULT_URL = "https://caldav.icloud.com/"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apple-id", required=True, help="Apple ID e-mail")
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument(
        "--password",
        help="App-specific password (omit to be prompted; recommended for shell-history safety)",
    )
    parser.add_argument("--days-ahead", type=int, default=30)
    args = parser.parse_args()

    # Loud runtime guard so nobody points this at the wrong account by reflex.
    print(
        "READ-ONLY smoke test. This script never writes. "
        "NEVER run against the owner's production account without explicit consent.",
        file=sys.stderr,
    )

    password = args.password or getpass.getpass("App-specific password: ")

    print(f"Connecting to {args.url} as {args.apple_id}...")
    client = caldav.DAVClient(url=args.url, username=args.apple_id, password=password)
    try:
        principal = client.principal()
    except Exception as e:
        print(f"FAIL Auth failed: {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    try:
        calendars = principal.calendars()
    except Exception as e:
        print(f"FAIL Could not list calendars: {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    names = [str(c.get_display_name()).strip() for c in calendars]
    print(f"OK   Authenticated. Found {len(names)} calendar(s): {', '.join(names) or '<none>'}")

    now = datetime.now(timezone.utc)
    end = now + timedelta(days=args.days_ahead)

    succeeded = 0
    skipped = 0
    for cal in calendars:
        name = str(cal.get_display_name()).strip() or "<unnamed>"
        try:
            raw = cal.search(start=now, end=end, event=True, expand=True)
        except Exception as e:
            # Count-only / type-only — never surface event content from a calendar
            # that failed (the error could echo other calendars' data).
            print(f"SKIP {name}: {type(e).__name__}: {e}")
            skipped += 1
            continue
        # COUNT ONLY — deliberately never print any VEVENT SUMMARY/LOCATION.
        print(f"OK   {name}: {len(list(raw))} events")
        succeeded += 1

    print(
        f"\nDone. {succeeded} calendar(s) searched OK, {skipped} skipped "
        f"(over the next {args.days_ahead} days). No data was written or removed."
    )
    return 0 if succeeded else 1


if __name__ == "__main__":
    sys.exit(main())
