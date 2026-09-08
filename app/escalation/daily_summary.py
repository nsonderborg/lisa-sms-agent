import logging
import zoneinfo
from datetime import datetime, timezone, timedelta

from app.config import OWNER_PHONE, MAX_SMS_LENGTH, SUMMARY_TIMES
from app.database import SessionLocal
from app.models import Booking, CalendarCache, Lead, SummaryLog
from app.sms.sender import send_sms

logger = logging.getLogger(__name__)

_TZ_COPENHAGEN = zoneinfo.ZoneInfo("Europe/Copenhagen")


def send_operational_summary(slot_label: str) -> None:
    """Send an operational summary SMS to the owner for the given time slot.

    slot_label: one of "06:00", "12:00", "17:00" (or whatever is in SUMMARY_TIMES)

    - Morning (first SUMMARY_TIMES entry): delta + today's schedule
    - Afternoon (last SUMMARY_TIMES entry): delta + next-day preview
    - Midday (middle entries): delta only
    """
    if not OWNER_PHONE:
        logger.warning("No OWNER_PHONE configured, skipping operational summary")
        return

    db = SessionLocal()
    try:
        # --- Determine since_dt ---
        last_log = (
            db.query(SummaryLog)
            .filter(SummaryLog.slot_label == slot_label)
            .order_by(SummaryLog.sent_at.desc())
            .first()
        )
        if last_log:
            since_dt = last_log.sent_at  # already naive UTC
        else:
            # Default: start of today in Copenhagen local time, converted to naive UTC
            now_local = datetime.now(_TZ_COPENHAGEN)
            today_start_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
            since_dt = today_start_local.astimezone(timezone.utc).replace(tzinfo=None)

        # --- Compute delta counts ---
        counts = _get_delta_counts(db, since_dt)

        # --- Build SMS ---
        lines = [f"[LISA {slot_label}]"]

        # Delta lines with emoji
        lines.append(f"\u2795 Nye bookinger: {counts['new_bookings']}")
        lines.append(f"\u274c Aflysninger: {counts['cancellations']}")
        lines.append(f"\U0001f464 Nye leads: {counts['new_leads']}")

        # Morning: add today's schedule
        if SUMMARY_TIMES and slot_label == SUMMARY_TIMES[0]:
            schedule_line = _get_today_schedule(db)
            lines.insert(1, schedule_line)  # after header

        # Afternoon: add next-day preview
        if SUMMARY_TIMES and slot_label == SUMMARY_TIMES[-1]:
            preview_line = _get_tomorrow_preview(db)
            lines.append(preview_line)

        summary_text = "\n".join(lines)

        # Enforce char limit
        if len(summary_text) > MAX_SMS_LENGTH:
            summary_text = summary_text[: MAX_SMS_LENGTH - 1] + "\u2026"

        # Send to owner
        send_sms(OWNER_PHONE, summary_text)
        logger.info("Operational summary sent to owner for slot %s", slot_label)

        # Record in SummaryLog
        log_entry = SummaryLog(
            slot_label=slot_label,
            sent_at=datetime.now(timezone.utc).replace(tzinfo=None),
        )
        db.add(log_entry)
        db.commit()

    except Exception:
        logger.exception("Failed to send operational summary for slot %s", slot_label)
    finally:
        db.close()


def _get_delta_counts(db, since_dt: datetime) -> dict:
    """Return counts of new bookings, cancellations, and new leads since since_dt."""
    new_bookings = (
        db.query(Booking)
        .filter(
            Booking.created_at >= since_dt,
            Booking.status != "cancelled",
        )
        .count()
    )

    cancellations = (
        db.query(Booking)
        .filter(
            Booking.cancelled_at >= since_dt,
            Booking.status == "cancelled",
        )
        .count()
    )

    new_leads = (
        db.query(Lead)
        .filter(Lead.created_at >= since_dt)
        .count()
    )

    return {
        "new_bookings": new_bookings,
        "cancellations": cancellations,
        "new_leads": new_leads,
    }


def _get_today_schedule(db) -> str:
    """Return a formatted line summarising today's schedule."""
    now_local = datetime.now(_TZ_COPENHAGEN)
    today_start_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    today_end_local = today_start_local + timedelta(days=1)

    today_start_utc = today_start_local.astimezone(timezone.utc).replace(tzinfo=None)
    today_end_utc = today_end_local.astimezone(timezone.utc).replace(tzinfo=None)

    # Count active bookings today by type
    bookings_today = (
        db.query(Booking)
        .filter(
            Booking.start_dt >= today_start_utc,
            Booking.start_dt < today_end_utc,
            Booking.status != "cancelled",
        )
        .all()
    )

    # Also count CalendarCache events today
    cal_events_today = (
        db.query(CalendarCache)
        .filter(
            CalendarCache.start_dt >= today_start_utc,
            CalendarCache.start_dt < today_end_utc,
        )
        .count()
    )

    if not bookings_today and not cal_events_today:
        return "\U0001f4c5 I dag: Ingen bookinger i dag"

    # Count by type
    type_counts: dict[str, int] = {}
    for b in bookings_today:
        type_counts[b.appointment_type] = type_counts.get(b.appointment_type, 0) + 1

    parts = []
    if type_counts.get("lesson"):
        parts.append(f"{type_counts['lesson']} koretime(r)")
    if type_counts.get("test"):
        parts.append(f"{type_counts['test']} prove")
    if type_counts.get("manoeuvring"):
        parts.append(f"{type_counts['manoeuvring']} manoeuvre")
    for t, c in type_counts.items():
        if t not in ("lesson", "test", "manoeuvring"):
            parts.append(f"{c} {t}")

    if cal_events_today and not bookings_today:
        parts.append(f"{cal_events_today} kalenderbegivenheder")

    summary = ", ".join(parts) if parts else f"{len(bookings_today)} booking(er)"
    return f"\U0001f4c5 I dag: {summary}"


def _get_tomorrow_preview(db) -> str:
    """Return a formatted line summarising tomorrow's appointments."""
    now_local = datetime.now(_TZ_COPENHAGEN)
    today_start_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    tomorrow_start_local = today_start_local + timedelta(days=1)
    tomorrow_end_local = tomorrow_start_local + timedelta(days=1)

    tomorrow_start_utc = tomorrow_start_local.astimezone(timezone.utc).replace(tzinfo=None)
    tomorrow_end_utc = tomorrow_end_local.astimezone(timezone.utc).replace(tzinfo=None)

    bookings_tomorrow = (
        db.query(Booking)
        .filter(
            Booking.start_dt >= tomorrow_start_utc,
            Booking.start_dt < tomorrow_end_utc,
            Booking.status != "cancelled",
        )
        .all()
    )

    cal_events_tomorrow = (
        db.query(CalendarCache)
        .filter(
            CalendarCache.start_dt >= tomorrow_start_utc,
            CalendarCache.start_dt < tomorrow_end_utc,
        )
        .count()
    )

    if not bookings_tomorrow and not cal_events_tomorrow:
        return "\U0001f4c5 I morgen: Ingen bookinger i morgen"

    type_counts: dict[str, int] = {}
    for b in bookings_tomorrow:
        type_counts[b.appointment_type] = type_counts.get(b.appointment_type, 0) + 1

    parts = []
    if type_counts.get("lesson"):
        parts.append(f"{type_counts['lesson']} koretime(r)")
    if type_counts.get("test"):
        parts.append(f"{type_counts['test']} prove")
    if type_counts.get("manoeuvring"):
        parts.append(f"{type_counts['manoeuvring']} manoeuvre")
    for t, c in type_counts.items():
        if t not in ("lesson", "test", "manoeuvring"):
            parts.append(f"{c} {t}")

    if cal_events_tomorrow and not bookings_tomorrow:
        parts.append(f"{cal_events_tomorrow} kalenderbegivenheder")

    summary = ", ".join(parts) if parts else f"{len(bookings_tomorrow)} booking(er)"
    return f"\U0001f4c5 I morgen: {summary}"


# ---------------------------------------------------------------------------
# Deprecated: kept for backwards compatibility — do not use in new code
# ---------------------------------------------------------------------------

def send_daily_summary():
    """Deprecated: replaced by send_operational_summary(). Do not use."""
    logger.warning("send_daily_summary() is deprecated; use send_operational_summary() instead")

    if not OWNER_PHONE:
        logger.warning("No OWNER_PHONE configured, skipping daily summary")
        return

    db = SessionLocal()
    try:
        from app.models import Escalation, Message

        today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        today_start_naive = today_start.replace(tzinfo=None)

        total_inbound = (
            db.query(Message)
            .filter(Message.direction == "inbound", Message.timestamp >= today_start_naive)
            .count()
        )

        new_leads = db.query(Lead).filter(Lead.created_at >= today_start_naive).count()
        unresolved = db.query(Escalation).filter(Escalation.resolved.is_(False)).count()

        lines = [
            "[LISA DAGSOVERSIGT]",
            f"Beskeder i dag: {total_inbound}",
            f"Nye leads: {new_leads}",
        ]
        if unresolved > 0:
            lines.append(f"Uløste eskaleringer: {unresolved}")

        send_sms(OWNER_PHONE, "\n".join(lines))
        logger.info("Daily summary sent to owner (deprecated path)")

    except Exception:
        logger.exception("Failed to send daily summary")
    finally:
        db.close()
