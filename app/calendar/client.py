"""Calendar backend dispatcher.

Re-exports the four calendar functions from whichever backend is selected in
``config.yaml`` (``calendar_backend: google | caldav``). Defaults to
``google`` so existing deployments keep working without config changes.

Existing code should import from here, not directly from ``gcal_client`` or
``caldav_client`` — that way each school's deployment can pick its backend
independently via the per-school ``config.yaml``.
"""
from app.config import CALENDAR_BACKEND

if CALENDAR_BACKEND == "caldav":
    from app.calendar.caldav_client import (
        CalendarSafetyError,
        build_calendar_service,
        create_event,
        delete_event,
        fetch_busy_intervals_all_calendars,
        fetch_events_for_window,
        normalise_event_times,
    )
else:
    # Sourced from caldav_client under BOTH backends, deliberately: the class
    # must have exactly ONE identity process-wide. Defining a parallel stub here
    # instead means `except CalendarSafetyError` in a module that imported the
    # dispatcher earlier silently stops matching as soon as anything re-imports
    # this module (the test suite does exactly that, and it cost a debugging
    # round). caldav_client imports only stdlib at module level, so this is
    # cheap and pulls in no CalDAV dependency. Never raised under google, which
    # deletes by event id against one explicit calendar id.
    from app.calendar.caldav_client import CalendarSafetyError
    from app.calendar.gcal_client import (
        build_calendar_service,
        create_event,
        delete_event,
        fetch_events_for_window,
        normalise_event_times,
    )

    def fetch_busy_intervals_all_calendars(days_ahead: int = 30):
        """Account-wide free/busy is CalDAV-only (locked decision).

        The google backend uses single-calendar fetch_events_for_window. This
        stub keeps `from app.calendar.client import fetch_busy_intervals_all_calendars`
        import-safe under google, but raises if ever actually called.
        """
        raise NotImplementedError(
            "Account-wide free/busy is CalDAV-only; the google backend "
            "uses single-calendar fetch_events_for_window."
        )

__all__ = [
    "CalendarSafetyError",
    "build_calendar_service",
    "create_event",
    "delete_event",
    "fetch_busy_intervals_all_calendars",
    "fetch_events_for_window",
    "normalise_event_times",
]
