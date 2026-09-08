"""Wipe events from the configured CalDAV (iCloud) calendar.

Pre-pilot cleanup: before real pilot students text Lisa, the owner's `Lisa`
calendar must be cleared of the test bookings made during owner-side testing.
Leftover events otherwise poll into the busy-cache and block genuine slots.
This is the calendar half of the pre-pilot wipe; the Postgres half (rows in
`conversations`, `messages`, `leads`, `bookings`, `calendar_cache`) is separate.

Targets whatever calendar `CALDAV_CALENDAR_NAME` resolves to (default `Lisa`),
so it naturally hits the right calendar once the production env var is set.

SAFE BY DEFAULT: a bare run only *lists* what it would delete (dry run).
Nothing is removed unless you pass --apply, and --apply asks you to confirm
by typing the calendar name (skip with --yes for non-interactive use).

Usage:
    # dry run — see what's there, delete nothing
    PYTHONPATH=. python3 scripts/wipe_test_calendar.py

    # actually delete (interactive confirm)
    PYTHONPATH=. python3 scripts/wipe_test_calendar.py --apply

    # only events whose summary contains a string (e.g. a test name)
    PYTHONPATH=. python3 scripts/wipe_test_calendar.py --apply --contains "Test Tester"

    # widen/narrow the search window (days relative to now)
    PYTHONPATH=. python3 scripts/wipe_test_calendar.py --days-back 730 --days-ahead 365

    # non-interactive (CI / scripted)
    PYTHONPATH=. python3 scripts/wipe_test_calendar.py --apply --yes
"""
import argparse
import os
import sys
from datetime import datetime, timedelta, timezone

os.environ["LISA_SKIP_VALIDATION"] = "1"

from dotenv import load_dotenv

load_dotenv(dotenv_path=".env", override=True)

from app.calendar.client import build_calendar_service  # noqa: E402


def _event_summary_and_start(raw) -> tuple[str, str]:
    """Best-effort (summary, start) for display, mirroring caldav_client parsing."""
    ical = getattr(raw, "icalendar_instance", None)
    if ical is None:
        return "(unparseable)", "?"
    for component in ical.walk("VEVENT"):
        summary = str(component.get("summary") or "(no summary)")
        dtstart = component.get("dtstart")
        start = str(dtstart.dt) if dtstart is not None else "?"
        return summary, start
    return "(no VEVENT)", "?"


def main() -> int:
    parser = argparse.ArgumentParser(description="Wipe events from the configured CalDAV calendar.")
    parser.add_argument("--apply", action="store_true", help="Actually delete (default is dry run).")
    parser.add_argument("--yes", action="store_true", help="Skip the interactive confirmation prompt.")
    parser.add_argument("--contains", default=None, help="Only events whose summary contains this string.")
    parser.add_argument("--days-back", type=int, default=730, help="Search window: days before now (default 730).")
    parser.add_argument("--days-ahead", type=int, default=365, help="Search window: days after now (default 365).")
    args = parser.parse_args()

    try:
        svc = build_calendar_service()
    except ValueError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 2

    cal_name = str(svc.get_display_name())
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=args.days_back)
    end = now + timedelta(days=args.days_ahead)

    print(f"Target calendar : {cal_name!r}")
    print(f"Window          : {start.date()} → {end.date()}")
    if args.contains:
        print(f"Summary filter  : contains {args.contains!r}")

    # expand=False so recurring events come back as deletable master objects,
    # not expanded per-occurrence instances (which can't be deleted directly).
    raw_events = svc.search(start=start, end=end, event=True, expand=False)

    targets = []
    for raw in raw_events:
        summary, ev_start = _event_summary_and_start(raw)
        if args.contains and args.contains not in summary:
            continue
        targets.append((raw, summary, ev_start))

    if not targets:
        print("\nNo matching events found. Nothing to do.")
        return 0

    print(f"\n{len(targets)} event(s) matched:")
    for _, summary, ev_start in targets:
        print(f"  • {ev_start}  {summary}")

    if not args.apply:
        print("\nDRY RUN — nothing deleted. Re-run with --apply to delete these.")
        return 0

    if not args.yes:
        print(
            f"\n⚠️  About to permanently delete {len(targets)} event(s) from {cal_name!r}."
        )
        typed = input(f"Type the calendar name ({cal_name!r}) to confirm: ").strip()
        if typed != cal_name:
            print("Confirmation did not match. Aborted — nothing deleted.")
            return 1

    deleted = 0
    failed = 0
    for raw, summary, ev_start in targets:
        try:
            raw.delete()
            deleted += 1
        except Exception as exc:  # noqa: BLE001 — best-effort; report and continue
            failed += 1
            print(f"  ✗ failed to delete {ev_start} {summary}: {exc}", file=sys.stderr)

    print(f"\n✅ Deleted {deleted} event(s) from {cal_name!r}." + (f" {failed} failed." if failed else ""))
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
