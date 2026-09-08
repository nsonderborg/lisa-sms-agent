import logging
import os
from contextlib import asynccontextmanager

from apscheduler.events import EVENT_JOB_ERROR
from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import Depends, FastAPI, HTTPException
from sqlalchemy.orm import Session

from app.booking.reminder import check_and_send_booking_reminders
from app.calendar.poller import poll_calendar
from app.calendar.reminder import check_and_send_reminders, check_no_reply_timeouts
from app.config import HEALTH_ALERT_ENABLED, SUMMARY_TIMES
from app.database import get_db, init_db
from app.escalation.daily_summary import send_operational_summary
from app.monitoring.health_alert import run_health_alert
from app.models import Booking, Conversation, Message
from app.sms.webhook import router as sms_router

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def _init_sentry() -> bool:
    """Initialise Sentry error tracking if SENTRY_DSN is set.

    No-op when the DSN is absent (local/dev/tests), so nothing changes unless
    the env var is provided in production. With fastapi installed, sentry-sdk
    auto-instruments request handling, so unhandled exceptions in the webhook
    are captured with full stack traces. Background APScheduler job failures
    are wired separately via an EVENT_JOB_ERROR listener (see lifespan).
    """
    dsn = os.environ.get("SENTRY_DSN", "").strip()
    if not dsn:
        logger.info("Sentry disabled (SENTRY_DSN not set)")
        return False
    import sentry_sdk

    sentry_sdk.init(
        dsn=dsn,
        environment=(
            os.environ.get("SENTRY_ENVIRONMENT")
            or os.environ.get("RAILWAY_ENVIRONMENT_NAME")
            or "production"
        ),
        traces_sample_rate=float(os.environ.get("SENTRY_TRACES_SAMPLE_RATE", "0.0")),
        # Keep PII off by default: SMS bodies/phone numbers can be personal data.
        send_default_pii=False,
    )
    logger.info("Sentry initialised (error tracking active)")
    return True


SENTRY_ENABLED = _init_sentry()

scheduler = BackgroundScheduler()


def _report_job_error(event) -> None:
    """Forward APScheduler job exceptions to Sentry (they are otherwise only logged)."""
    if event.exception and SENTRY_ENABLED:
        import sentry_sdk

        sentry_sdk.capture_exception(event.exception)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()

    # Poll iCal and send reminders every 15 minutes
    scheduler.add_job(check_and_send_reminders, "interval", minutes=15, id="reminder_poll")
    # Poll Booking table for 24h reminders (alongside iCal reminders, per D-10)
    scheduler.add_job(check_and_send_booking_reminders, "interval", minutes=15, id="booking_reminder_poll")
    # Check for no-reply timeouts every 30 minutes
    scheduler.add_job(check_no_reply_timeouts, "interval", minutes=30, id="no_reply_check")

    # Operational summaries to owner (3x/day: morning, midday, afternoon)
    for i, time_str in enumerate(SUMMARY_TIMES or []):
        hour, minute = (int(x) for x in time_str.split(":"))
        scheduler.add_job(
            send_operational_summary,
            "cron",
            hour=hour,
            minute=minute,
            timezone="Europe/Copenhagen",
            id=f"summary_{i}",
            kwargs={"slot_label": time_str},
        )

    # Poll the calendar backend every 15 minutes. Shrinks the busy-cache
    # staleness window from ~8h (old 3x/day cron) to ~15min, which closes the
    # double-booking race when the owner hand-adds an event to the calendar
    # between polls. CalDAV/Google polling is free (no quota) and runs
    # server-side, so the frequency cost is negligible. (Bookings Lisa makes are
    # already protected live via the Booking table; this covers external edits.)
    scheduler.add_job(
        poll_calendar,
        "interval",
        minutes=15,
        timezone="Europe/Copenhagen",
        id="gcal_poll",
    )
    # Warm the cache once at startup so the first booking after a deploy isn't
    # served against an empty cache (the cron above only fires on the hour).
    # Runs on the scheduler thread, so app startup is not blocked.
    scheduler.add_job(poll_calendar, id="gcal_poll_startup")

    # Failure-only daily health ping: SMS OWNER_PHONE only if the calendar poll
    # has gone stale or the DB is unreachable. Silent when healthy.
    # Gated: with no pilot running, a permanently-dead calendar credential makes
    # this fire every single day about a condition nobody intends to fix yet.
    if HEALTH_ALERT_ENABLED:
        scheduler.add_job(
            run_health_alert,
            "cron",
            hour=7,
            minute=30,
            timezone="Europe/Copenhagen",
            id="daily_health_alert",
        )
    else:
        logger.warning(
            "HEALTH_ALERT_ENABLED=0 — daily health ping is OFF. Nothing will tell you "
            "the calendar poll has wedged. Set it back to 1 before any pilot goes live."
        )

    # Report background-job failures to Sentry (no-op if Sentry is disabled).
    scheduler.add_listener(_report_job_error, EVENT_JOB_ERROR)

    scheduler.start()
    logger.info(
        "Scheduler started: reminder poll (15m), booking reminder poll (15m), no-reply check (30m), operational summaries (%s CET), calendar poll (15m), daily health alert (07:30 CET)",
        ", ".join(SUMMARY_TIMES or []),
    )

    yield

    scheduler.shutdown(wait=False)
    logger.info("Scheduler shut down")


app = FastAPI(title="Lisa — Driving School Secretary", lifespan=lifespan)
app.include_router(sms_router)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/admin/reset")
def admin_reset(phone: str, token: str, db: Session = Depends(get_db)):
    """Reset a student's conversation state. Requires ADMIN_TOKEN query param."""
    admin_token = os.environ.get("ADMIN_TOKEN", "")
    if not admin_token or token != admin_token:
        raise HTTPException(status_code=403, detail="Forbidden")
    convo = db.query(Conversation).filter(Conversation.phone_number == phone).first()
    if not convo:
        return {"reset": False, "detail": f"No conversation found for {phone}"}
    db.query(Message).filter(Message.conversation_id == convo.id).delete()
    db.query(Booking).filter(Booking.conversation_id == convo.id).delete()
    db.delete(convo)
    db.commit()
    return {"reset": True, "phone": phone}
