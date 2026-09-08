"""Smoke-test the CalDAV calendar backend.

Runs the full create → fetch → delete round-trip against the configured
CalDAV calendar (typically iCloud). Prints each step so failures are
easy to localise.

Usage:
    PYTHONPATH=. python3 scripts/test_caldav.py
"""
import os

os.environ["LISA_SKIP_VALIDATION"] = "1"

from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

load_dotenv(dotenv_path=".env", override=True)

from app.calendar.client import (  # noqa: E402  (import after env load)
    build_calendar_service,
    create_event,
    delete_event,
    fetch_events_for_window,
)


def main() -> None:
    svc = build_calendar_service()
    print(f"Connected to calendar: {svc.get_display_name()}")

    start = datetime.now(timezone.utc) + timedelta(hours=1)
    end = start + timedelta(minutes=30)
    ev = create_event(
        svc,
        "_ignored",
        summary="Lisa smoke test",
        start_dt=start,
        end_dt=end,
    )
    print(f"Created event id (URL): {ev['id']}")

    events = fetch_events_for_window(svc, "_ignored", days_ahead=2)
    found = any(e.get("summary") == "Lisa smoke test" for e in events)
    print(f"Fetched {len(events)} events; ours present: {found}")

    delete_event(svc, "_ignored", ev["id"])
    print("Deleted.")


if __name__ == "__main__":
    main()
