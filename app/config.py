import os
import sys
from datetime import date
from pathlib import Path

import yaml
from dotenv import load_dotenv

load_dotenv()

# --- Environment variables (secrets) ---
# Use os.environ.get() for all env vars so validation can produce human-readable
# errors instead of raw KeyErrors.
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "")
TWILIO_PHONE_NUMBER = os.environ.get("TWILIO_PHONE_NUMBER", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
OWNER_PHONE = os.environ.get("OWNER_PHONE", "")
# Optional second recipient for booking/cancellation alerts (the school owner,
# e.g. the owner). During the white-glove pilot OWNER_PHONE stays on the developer
# (full operational firehose); this CCs the school owner on just the
# bookings/cancellations they must act on. Unset (default) = no extra SMS,
# behaviour unchanged. Kept in env (not config.yaml) so the number stays out of
# the repo, like BETA_PHONE_ALLOWLIST.
SCHOOL_OWNER_PHONE = os.environ.get("SCHOOL_OWNER_PHONE", "")
# Hard cap on how many FUTURE confirmed bookings a single student may hold at
# once (anti calendar-spam during the priority-booking pilot). 0 = no cap
# (default — preserves existing behaviour and tests). Set MAX_OUTSTANDING_BOOKINGS=2
# on Railway for the pilot. Enforced at the start of the booking flow.
MAX_OUTSTANDING_BOOKINGS = int(os.environ.get("MAX_OUTSTANDING_BOOKINGS", "0"))
# Hard cap on how many confirmed bookings a single student may hold on the SAME
# calendar day (so a student can't soak up two slots in one day). 0 = no cap
# (default — preserves existing behaviour and tests). Set MAX_BOOKINGS_PER_DAY=1
# on Railway for the pilot. Enforced at booking-confirm time against the chosen
# slot's date. Independent of MAX_OUTSTANDING_BOOKINGS (the total-outstanding cap).
MAX_BOOKINGS_PER_DAY = int(os.environ.get("MAX_BOOKINGS_PER_DAY", "0"))
DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///lisa.db")  # Railway injects postgresql:// in production
ICAL_URL = os.environ.get("ICAL_URL", "")

# --- Beta allowlist gate (Phase 999.28) ---
# Comma-separated E.164 phone numbers permitted to reach Lisa's pipeline
# during pilot phase. Empty / unset = open to all (production default,
# current behavior preserved). Populated = beta-gated; only OWNER_PHONE
# and listed numbers reach the conversation / intent / LLM / DB pipeline.
# Parsed once at module load into a frozenset for O(1) membership checks.
BETA_PHONE_ALLOWLIST = frozenset(
    p.strip()
    for p in os.environ.get("BETA_PHONE_ALLOWLIST", "").split(",")
    if p.strip()
)

# --- Daily health alert ---
# The failure-only health ping (app/monitoring/health_alert.py) assumes a live
# pilot: a stale calendar cache means a real instructor is about to get bad slot
# offers. With no active pilot the same condition is expected rather than urgent,
# and a daily SMS about it is noise that trains you to ignore the channel — the
# exact failure the alert was designed to avoid.
# Default ON: monitoring must never disappear by omission. Set to "0" only while
# no pilot is running, and set it back to "1" when one starts.
HEALTH_ALERT_ENABLED = os.environ.get("HEALTH_ALERT_ENABLED", "1") != "0"

# --- School config (config.yaml) ---
# Resolution order: env var CONFIG_PATH > repo-root config.yaml.
# One deploy serves exactly one school, so onboarding a second school means a
# second service pointed at its own config file \u2014 not a code change and not a
# shared deploy. Mirrors the CALENDAR_BACKEND / BOOKING_*_DATE idiom above: the
# env var lets a Railway service override the committed default without
# polluting master with per-school values.
# A relative CONFIG_PATH resolves against the repo root, so `config.oemer.yaml`
# works regardless of the process's working directory (Railway's differs from a
# local shell's). An ABSOLUTE CONFIG_PATH is honoured as-is \u2014 pathlib's `/`
# discards the left operand when the right side is absolute \u2014 which is what a
# mounted volume needs.
_repo_root = Path(__file__).resolve().parent.parent
_config_override = os.environ.get("CONFIG_PATH", "").strip()
_config_path = _repo_root / (_config_override or "config.yaml")

try:
    with open(_config_path) as f:
        SCHOOL_CONFIG = yaml.safe_load(f) or {}
except FileNotFoundError:
    print(f"\u274c Config error: school config not found at {_config_path}")
    if _config_override:
        # Name the override explicitly: a typo'd CONFIG_PATH is otherwise
        # indistinguishable from a missing file, and this is read at import
        # time \u2014 i.e. the process dies at boot with only this line to go on.
        print(f"  CONFIG_PATH={_config_override!r} is set. Check the filename,")
        print("  and that the file is committed (Railway builds from the repo).")
    else:
        print("  Run: python scripts/setup.py   to create it.")
    sys.exit(1)

SCHOOL_NAME = SCHOOL_CONFIG.get("school_name", "")
SCHOOL_ADDRESS = SCHOOL_CONFIG.get("address", "")
SCHOOL_CITY = SCHOOL_CONFIG.get("city", "")
THEORY_SCHEDULE = SCHOOL_CONFIG.get("theory_schedule", "")
PRICING = SCHOOL_CONFIG.get("pricing", "")
LICENSE_TYPES = SCHOOL_CONFIG.get("license_types", ["B"])
LANGUAGE_DEFAULT = SCHOOL_CONFIG.get("language_default", "da")
CALENDAR_PHONE_PATTERN = SCHOOL_CONFIG.get("calendar_phone_pattern", r"\+45\d{8}")
# FOOTGUN: config.yaml ships `summary_times:` with its entries commented out,
# which YAML parses as an EXPLICIT None — the key exists, so this .get() default
# never applies and SUMMARY_TIMES is None (summaries off). Every consumer must
# tolerate None (main.py iterates `SUMMARY_TIMES or []`). To re-enable summaries,
# uncomment the list entries in config.yaml.
SUMMARY_TIMES = SCHOOL_CONFIG.get("summary_times", ["06:00", "12:00", "17:00"])
DAILY_SUMMARY_TIME = SUMMARY_TIMES[0] if SUMMARY_TIMES else "08:00"  # compatibility alias
REMINDER_HOURS_BEFORE = SCHOOL_CONFIG.get("reminder_hours_before", 24)
NO_REPLY_TIMEOUT_HOURS = SCHOOL_CONFIG.get("no_reply_timeout_hours", 4)

# --- Calendar backend selector ---
# Resolution order: env var CALENDAR_BACKEND > config.yaml calendar_backend > "google".
# Env var lets a Railway deployment override the committed config (e.g. flip to
# caldav for a personal test calendar) without polluting master with per-env values.
CALENDAR_BACKEND = (
    os.environ.get("CALENDAR_BACKEND")
    or SCHOOL_CONFIG.get("calendar_backend")
    or "google"
).strip().lower()

# --- Google Calendar config (used when calendar_backend == "google") ---
# Auth method 1: service account JSON (set GOOGLE_SERVICE_ACCOUNT_JSON)
# Auth method 2: OAuth2 refresh token (set GOOGLE_CLIENT_ID + GOOGLE_CLIENT_SECRET + GOOGLE_REFRESH_TOKEN)
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
GOOGLE_REFRESH_TOKEN = os.environ.get("GOOGLE_REFRESH_TOKEN", "")
GCAL_CALENDAR_ID = SCHOOL_CONFIG.get("gcal_calendar_id", "")
CALENDAR_POLL_HOURS = SCHOOL_CONFIG.get("calendar_poll_hours", "8,13,17")
CALENDAR_CACHE_DAYS_AHEAD = int(SCHOOL_CONFIG.get("calendar_cache_days_ahead", 30))

# --- CalDAV config (used when calendar_backend == "caldav") ---
# URL defaults to iCloud; teacher provides Apple ID + app-specific password
# generated at appleid.apple.com → Sign-In and Security → App-Specific Passwords.
CALDAV_URL = os.environ.get("CALDAV_URL", "https://caldav.icloud.com/")
CALDAV_USERNAME = os.environ.get("CALDAV_USERNAME", "")
CALDAV_PASSWORD = os.environ.get("CALDAV_PASSWORD", "")
CALDAV_CALENDAR_NAME = (
    os.environ.get("CALDAV_CALENDAR_NAME")
    or SCHOOL_CONFIG.get("caldav_calendar_name")
    or "Lisa"
)
WORK_START = SCHOOL_CONFIG.get("work_start", "08:00")
WORK_END = SCHOOL_CONFIG.get("work_end", "18:00")
# Minimum lead time before a bookable slot — the "front edge" of the bookable
# window, complementing work_end's back edge. Lisa won't offer a slot starting
# sooner than this many hours from now, so a same-day "ASAP" request can't land
# minutes away on the instructor's unblocked time. 0 = off (back-compat).
# Env-overridable (MIN_NOTICE_HOURS) for quick prod tuning once the owner states
# his preferred notice; otherwise read from config.yaml min_notice_hours.
MIN_NOTICE_HOURS = float(
    os.environ.get("MIN_NOTICE_HOURS") or SCHOOL_CONFIG.get("min_notice_hours", 0)
)


def _parse_floor_date(raw) -> date | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


# Absolute earliest-bookable date (ISO yyyy-mm-dd). Lisa won't offer any slot
# before this date — a hard launch/pause gate independent of the rolling
# min-notice. Used e.g. to launch the pilot while the owner's car is still at the
# shop: bookings can't land before the car is back. Empty = off. CLEAR it (unset
# the env var / blank the config) once the gate no longer applies.
# Env-overridable (BOOKING_FLOOR_DATE) over config.yaml booking_floor_date.
BOOKING_FLOOR_DATE = _parse_floor_date(
    os.environ.get("BOOKING_FLOOR_DATE") or SCHOOL_CONFIG.get("booking_floor_date", "")
)

# Absolute latest-bookable cutoff (ISO yyyy-mm-dd). Lisa won't offer any slot ON or
# AFTER this date — a hard upper bound mirroring BOOKING_FLOOR_DATE. Used to cap the
# window when the owner is unavailable (e.g. vacation): set it to the FIRST
# unavailable day (the day after the last working day). Empty = off.
# NOTE: prefer this over calendar-blocking for multi-day gaps — the busy-cache reader
# skips all-day events, so an all-day "Ferie" block does NOT stop Lisa.
# Env-overridable (BOOKING_CEILING_DATE) over config.yaml booking_ceiling_date.
BOOKING_CEILING_DATE = _parse_floor_date(
    os.environ.get("BOOKING_CEILING_DATE") or SCHOOL_CONFIG.get("booking_ceiling_date", "")
)
APPOINTMENT_TYPES = SCHOOL_CONFIG.get("appointment_types", {})
FAQ = SCHOOL_CONFIG.get("faq", {})

# 999.37 pilot seed — official lektionsplan-number → content-label map, used to
# nudge a student to verify their position at first-booking onboarding. Keys are
# coerced to int (YAML usually parses them as ints already); a malformed key is
# skipped rather than crashing config load.
def _load_lektionsplan_labels() -> dict[int, str]:
    raw = SCHOOL_CONFIG.get("lektionsplan_labels", {}) or {}
    labels: dict[int, str] = {}
    for k, v in raw.items():
        try:
            labels[int(k)] = str(v)
        except (ValueError, TypeError):
            continue
    return labels


LEKTIONSPLAN_LABELS = _load_lektionsplan_labels()


# Lessons Lisa must NOT auto-book — she tells the student to arrange them directly
# with the instructor and notifies the owner. Default {44} = mørkekørsel (darkness
# driving): evening/sunset-dependent, owner books it by hand (the owner, 2026-06-16).
def _load_manual_booking_lessons() -> set[int]:
    out: set[int] = set()
    for n in SCHOOL_CONFIG.get("manual_booking_lessons", [44]) or []:
        try:
            out.add(int(n))
        except (ValueError, TypeError):
            continue
    return out


MANUAL_BOOKING_LESSONS = _load_manual_booking_lessons()

# --- Constants ---
MAX_SMS_LENGTH = 320
TARGET_SMS_LENGTH = 160
MAX_AUTOMATED_MESSAGES_PER_DAY = int(os.environ.get("MAX_MESSAGES_PER_DAY", "3"))
CONFIDENCE_THRESHOLD = 0.7
CLASSIFICATION_MODEL = "claude-haiku-4-5-20251001"


# ---------------------------------------------------------------------------
# Startup validation
# ---------------------------------------------------------------------------

def validate_config() -> list[str]:
    """Validate all required config fields.

    Returns a list of human-readable error strings.
    An empty list means the configuration is valid.

    Checks:
    - Required env vars (D-01, D-03): must be non-empty strings.
    - Required config.yaml fields (D-02): school_name, gcal_calendar_id.
    - Optional fields are NOT checked (theory_schedule, pricing, faq, etc.).

    Error messages identify the field NAME only — never the value (T-10-02).
    """
    errors: list[str] = []

    # Required env vars (per D-01, D-03)
    required_env = [
        "TWILIO_ACCOUNT_SID",
        "TWILIO_AUTH_TOKEN",
        "TWILIO_PHONE_NUMBER",
        "ANTHROPIC_API_KEY",
        "OWNER_PHONE",
    ]
    for var in required_env:
        val = os.environ.get(var, "")
        if not val.strip():
            errors.append(f"env var {var} is not set")

    # Calendar credentials — backend-specific (per D-02)
    backend = (SCHOOL_CONFIG.get("calendar_backend") or "google").strip().lower()
    if backend == "google":
        has_sa = bool(os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip())
        has_oauth = all(
            os.environ.get(v, "").strip()
            for v in ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_REFRESH_TOKEN")
        )
        if not has_sa and not has_oauth:
            errors.append(
                "calendar_backend=google but no credentials: set "
                "GOOGLE_SERVICE_ACCOUNT_JSON, or GOOGLE_CLIENT_ID + "
                "GOOGLE_CLIENT_SECRET + GOOGLE_REFRESH_TOKEN"
            )
        if not str(SCHOOL_CONFIG.get("gcal_calendar_id", "") or "").strip():
            errors.append(
                "'gcal_calendar_id' in config.yaml is required when "
                "calendar_backend=google"
            )
    elif backend == "caldav":
        for v in ("CALDAV_USERNAME", "CALDAV_PASSWORD"):
            if not os.environ.get(v, "").strip():
                errors.append(
                    f"calendar_backend=caldav but env var {v} is not set"
                )
        if not str(SCHOOL_CONFIG.get("caldav_calendar_name", "") or "Lisa").strip():
            errors.append(
                "'caldav_calendar_name' in config.yaml is required when "
                "calendar_backend=caldav"
            )
    else:
        errors.append(
            f"calendar_backend in config.yaml is {backend!r}; must be "
            f"'google' or 'caldav'"
        )

    if not str(SCHOOL_NAME).strip():
        errors.append("'school_name' in config.yaml is required but empty")

    # Unfilled placeholders (D-02, extended). A half-written school config passes
    # every emptiness check above — "TODO: exact registered name" is a non-empty
    # string — and then boots cleanly and answers real students with it. That is
    # worse than not booting: the failure is invisible until a student reads it.
    # Only the required fields are checked; optional ones stay the operator's
    # problem, per the "optional fields are NOT checked" rule above.
    for field, value in (("school_name", SCHOOL_NAME), ("gcal_calendar_id", GCAL_CALENDAR_ID)):
        if str(value).strip().upper().startswith("TODO"):
            errors.append(
                f"'{field}' in config.yaml is still an unfilled TODO placeholder"
            )

    return errors


def _enforce_config() -> None:
    """Print config errors and exit with code 1 if any are found.

    Called at module import time (unless LISA_SKIP_VALIDATION=1).
    Intentionally causes sys.exit(1) to prevent the server from starting
    with an invalid configuration (T-10-03: DoS accepted by design).
    """
    errors = validate_config()
    if errors:
        for err in errors:
            print(f"\u274c Config error: {err}")
        sys.exit(1)


# Run validation at import time unless explicitly skipped.
# Set LISA_SKIP_VALIDATION=1 in tests and scripts/setup.py to import
# validate_config() without triggering sys.exit().
if os.environ.get("LISA_SKIP_VALIDATION") != "1":
    _enforce_config()
