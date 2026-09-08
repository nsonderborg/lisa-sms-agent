"""Failure-only daily health ping for the pilot.

Runs daily via APScheduler (see app/main.py). SILENT when healthy; SMSes
OWNER_PHONE only when something is degraded — so the alert stays meaningful
(a daily "all good" trains you to ignore it; silence-unless-broken does not).

Runs INSIDE the app, so the DB and CalDAV creds are already available — no
public-URL gymnastics (unlike the on-demand .claude/skills/lisa-healthcheck
script, which runs off-Railway).

Checks the two silent-failure modes that matter during the pilot:
  1. Calendar poll wedged -> stale busy-cache -> bad/empty slot offers.
  2. DB unreachable.
"""
import logging
from datetime import datetime

from sqlalchemy import func

from app.config import OWNER_PHONE
from app.database import SessionLocal
from app.models import CalendarCache
from app.sms.sender import send_sms

logger = logging.getLogger(__name__)

# The calendar poll runs every 15 min. 45 min = 3 consecutive missed polls,
# i.e. the job is genuinely wedged rather than a single transient miss.
CACHE_STALE_MIN = 45


def _collect_problems() -> list[str]:
    problems: list[str] = []
    try:
        db = SessionLocal()
        try:
            fetched = db.query(func.max(CalendarCache.fetched_at)).scalar()
            if fetched is None:
                problems.append("calendar cache EMPTY (poll never ran)")
            else:
                age_min = (datetime.utcnow() - fetched).total_seconds() / 60
                if age_min > CACHE_STALE_MIN:
                    problems.append(
                        f"calendar poll stale: {age_min:.0f} min since last poll "
                        f"(threshold {CACHE_STALE_MIN})"
                    )
        finally:
            db.close()
    except Exception as e:  # DB unreachable / query failed
        problems.append(f"DB error: {str(e)[:80]}")
    return problems


def run_health_alert() -> None:
    """Daily check; SMS OWNER_PHONE only if degraded."""
    problems = _collect_problems()
    if not problems:
        logger.info("Daily health ping: all green (no alert sent)")
        return

    msg = "⚠️ LISA HEALTH (auto): " + "; ".join(problems)
    logger.warning("Daily health ping DEGRADED: %s", "; ".join(problems))
    if not OWNER_PHONE:
        logger.warning("No OWNER_PHONE configured, cannot send health alert")
        return
    try:
        send_sms(OWNER_PHONE, msg[:320])
    except Exception as e:
        logger.error("Failed to send health alert SMS: %s", e)
