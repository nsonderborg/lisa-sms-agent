"""
Tests for the operational summary system (3x/day summaries).

Tests cover:
- SummaryLog model creation and queries
- SUMMARY_TIMES config loading
- Scheduler job registration (3 jobs with summary_* ids)
- Delta counts (bookings, cancellations, leads)
- Morning summary includes today's schedule
- Afternoon summary includes next-day preview
- Midday summary is delta-only
- SummaryLog is recorded after each summary
- Summary always sent to OWNER_PHONE, never student phone
- SMS format has emoji header [LISA HH:MM]
"""
import os

os.environ.setdefault("TWILIO_ACCOUNT_SID", "ACtest00000000000000000000000000000")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test_auth_token_placeholder")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "+15005550006")
os.environ.setdefault("ANTHROPIC_API_KEY", "test_anthropic_key")
os.environ.setdefault("OWNER_PHONE", "+4500000000")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models import Base, Booking, Conversation, Lead, SummaryLog


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def db_session():
    engine = create_engine(
        "sqlite:///:memory:",
        echo=False,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(engine)
        engine.dispose()


def _make_booking(db_session, created_offset_hours=0, cancelled=False, start_offset_hours=0):
    """Helper: create a Booking relative to now."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    created_at = now + timedelta(hours=created_offset_hours)
    cancelled_at = created_at if cancelled else None

    # Booking requires a conversation_id (nullable=False)
    import uuid
    conv = Conversation(phone_number=f"+451{uuid.uuid4().int}"[:13], state="idle")
    db_session.add(conv)
    db_session.flush()

    b = Booking(
        phone_number="+4511111111",
        conversation_id=conv.id,
        appointment_type="lesson",
        start_dt=now + timedelta(hours=start_offset_hours),
        end_dt=now + timedelta(hours=start_offset_hours + 1),
        status="cancelled" if cancelled else "confirmed",
        created_at=created_at,
        cancelled_at=cancelled_at,
    )
    db_session.add(b)
    db_session.commit()
    return b


def _make_lead(db_session, created_offset_hours=0):
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    lead = Lead(
        phone_number="+4522222222",
        created_at=now + timedelta(hours=created_offset_hours),
    )
    db_session.add(lead)
    db_session.commit()
    return lead


# ---------------------------------------------------------------------------
# Task 1 tests: SummaryLog model and SUMMARY_TIMES config
# ---------------------------------------------------------------------------

class TestSummaryLogModel:
    def test_summary_log_can_be_created_and_queried(self, db_session):
        """SummaryLog model has id, slot_label, sent_at columns."""
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        log = SummaryLog(slot_label="06:00", sent_at=now)
        db_session.add(log)
        db_session.commit()

        result = db_session.query(SummaryLog).filter_by(slot_label="06:00").first()
        assert result is not None
        assert result.slot_label == "06:00"
        assert result.sent_at is not None
        assert result.id is not None

    def test_summary_log_multiple_slots(self, db_session):
        """Multiple SummaryLog entries can exist for different slots."""
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        for slot in ["06:00", "12:00", "17:00"]:
            db_session.add(SummaryLog(slot_label=slot, sent_at=now))
        db_session.commit()

        count = db_session.query(SummaryLog).count()
        assert count == 3


class TestSummaryTimesConfig:
    def test_summary_times_none_or_valid_time_strings(self):
        """SUMMARY_TIMES is None (summaries off) or a list of valid HH:MM strings.

        config.yaml currently ships with `summary_times:` present but all its
        entries commented out — YAML parses that as None (summaries disabled
        for the single-student pilot). Both states must be tolerated: main.py
        iterates `SUMMARY_TIMES or []`. Do NOT assert specific times here —
        that just re-breaks whenever the config is toggled.
        """
        from app.config import SUMMARY_TIMES
        if SUMMARY_TIMES is None:
            return  # summaries off — a valid, deliberate state
        assert isinstance(SUMMARY_TIMES, list)
        assert len(SUMMARY_TIMES) >= 1
        for t in SUMMARY_TIMES:
            hour, minute = t.split(":")
            assert 0 <= int(hour) <= 23
            assert 0 <= int(minute) <= 59


def _run_lifespan(mocker, summary_times):
    """Run main.py's real lifespan startup with a mocked scheduler.

    Returns the mock scheduler so tests can inspect the actual add_job calls
    made by app startup (the previous tests simulated the registration loop
    inside the test body, which asserted nothing about main.py).
    """
    import asyncio

    mock_scheduler = MagicMock()
    mocker.patch("app.main.scheduler", mock_scheduler)
    mocker.patch("app.main.init_db")
    mocker.patch("app.main.SUMMARY_TIMES", summary_times)

    from app.main import app, lifespan

    async def _startup_and_shutdown():
        async with lifespan(app):
            pass

    asyncio.run(_startup_and_shutdown())
    return mock_scheduler


class TestSchedulerJobRegistration:
    def test_summary_jobs_registered_from_config(self, mocker):
        """Startup registers one cron job per SUMMARY_TIMES entry with the
        summary_N id, the right hour/minute, and the slot label kwarg."""
        scheduler = _run_lifespan(mocker, ["06:00", "12:00", "17:00"])

        summary_calls = {
            call.kwargs["id"]: call
            for call in scheduler.add_job.call_args_list
            if str(call.kwargs.get("id", "")).startswith("summary_")
        }

        assert sorted(summary_calls) == ["summary_0", "summary_1", "summary_2"]
        for i, (hour, label) in enumerate([(6, "06:00"), (12, "12:00"), (17, "17:00")]):
            call = summary_calls[f"summary_{i}"]
            assert call.kwargs["hour"] == hour
            assert call.kwargs["minute"] == 0
            assert call.kwargs["timezone"] == "Europe/Copenhagen"
            assert call.kwargs["kwargs"] == {"slot_label": label}

        # The old single daily_summary job must not come back
        all_ids = [c.kwargs.get("id") for c in scheduler.add_job.call_args_list]
        assert "daily_summary" not in all_ids

    def test_no_summary_jobs_when_summary_times_none(self, mocker):
        """SUMMARY_TIMES=None (summaries off in config.yaml) must not crash
        startup and must register zero summary jobs — the other background
        jobs still register."""
        scheduler = _run_lifespan(mocker, None)

        all_ids = [c.kwargs.get("id") for c in scheduler.add_job.call_args_list]
        assert not any(str(i).startswith("summary_") for i in all_ids)
        assert "reminder_poll" in all_ids  # startup otherwise intact


# ---------------------------------------------------------------------------
# Task 2 tests: send_operational_summary delta logic, schedule, preview
# ---------------------------------------------------------------------------

class TestDeltaCounts:
    def test_delta_counts_new_bookings(self, db_session, mocker):
        """Delta counts: 2 bookings after since_dt, 1 before -> count is 2."""
        from app.escalation.daily_summary import _get_delta_counts
        since_dt = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=1)
        _make_booking(db_session, created_offset_hours=0)   # after
        _make_booking(db_session, created_offset_hours=0)   # after
        _make_booking(db_session, created_offset_hours=-2)  # before

        counts = _get_delta_counts(db_session, since_dt)
        assert counts["new_bookings"] == 2

    def test_delta_counts_cancellations(self, db_session):
        """Delta counts: booking cancelled after since_dt -> cancellations count is 1."""
        from app.escalation.daily_summary import _get_delta_counts
        since_dt = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=1)
        _make_booking(db_session, created_offset_hours=-2, cancelled=True)   # cancelled before
        _make_booking(db_session, created_offset_hours=0, cancelled=True)    # cancelled after

        counts = _get_delta_counts(db_session, since_dt)
        assert counts["cancellations"] == 1

    def test_delta_counts_new_leads(self, db_session):
        """Delta counts: 3 leads created after since_dt -> count is 3."""
        from app.escalation.daily_summary import _get_delta_counts
        since_dt = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=1)
        for _ in range(3):
            _make_lead(db_session, created_offset_hours=0)
        _make_lead(db_session, created_offset_hours=-2)  # before

        counts = _get_delta_counts(db_session, since_dt)
        assert counts["new_leads"] == 3


class TestMorningSummary:
    def test_morning_includes_schedule(self, db_session, mocker):
        """Morning summary (slot_label='06:00') includes today's schedule.

        SUMMARY_TIMES is pinned: morning/afternoon extras key off the
        first/last entry, and prod config currently has summaries off (None).
        """
        mock_send = mocker.patch("app.escalation.daily_summary.send_sms")
        mocker.patch(
            "app.escalation.daily_summary.SUMMARY_TIMES", ["06:00", "12:00", "17:00"]
        )
        mocker.patch(
            "app.escalation.daily_summary.SessionLocal",
            return_value=db_session,
        )

        from app.escalation.daily_summary import send_operational_summary
        send_operational_summary(slot_label="06:00")

        assert mock_send.called
        sms_text = mock_send.call_args[0][1]
        # Morning should include "I dag" (today's schedule) or "Ingen bookinger i dag"
        assert "I dag" in sms_text or "dag" in sms_text.lower()

    def test_morning_header_format(self, db_session, mocker):
        """Morning summary starts with [LISA 06:00] header."""
        mock_send = mocker.patch("app.escalation.daily_summary.send_sms")
        mocker.patch(
            "app.escalation.daily_summary.SessionLocal",
            return_value=db_session,
        )

        from app.escalation.daily_summary import send_operational_summary
        send_operational_summary(slot_label="06:00")

        sms_text = mock_send.call_args[0][1]
        assert "[LISA 06:00]" in sms_text


class TestAfternoonSummary:
    def test_afternoon_includes_preview(self, db_session, mocker):
        """Afternoon summary (slot_label='17:00') includes next-day preview.

        SUMMARY_TIMES pinned — see test_morning_includes_schedule.
        """
        mock_send = mocker.patch("app.escalation.daily_summary.send_sms")
        mocker.patch(
            "app.escalation.daily_summary.SUMMARY_TIMES", ["06:00", "12:00", "17:00"]
        )
        mocker.patch(
            "app.escalation.daily_summary.SessionLocal",
            return_value=db_session,
        )

        from app.escalation.daily_summary import send_operational_summary
        send_operational_summary(slot_label="17:00")

        sms_text = mock_send.call_args[0][1]
        # Afternoon should include "I morgen" (tomorrow's preview)
        assert "I morgen" in sms_text or "morgen" in sms_text.lower()

    def test_afternoon_header_format(self, db_session, mocker):
        """Afternoon summary has [LISA 17:00] header."""
        mock_send = mocker.patch("app.escalation.daily_summary.send_sms")
        mocker.patch(
            "app.escalation.daily_summary.SessionLocal",
            return_value=db_session,
        )

        from app.escalation.daily_summary import send_operational_summary
        send_operational_summary(slot_label="17:00")

        sms_text = mock_send.call_args[0][1]
        assert "[LISA 17:00]" in sms_text


class TestMiddaySummary:
    def test_midday_delta_only(self, db_session, mocker):
        """Midday summary (slot_label='12:00') does NOT include schedule or preview."""
        mock_send = mocker.patch("app.escalation.daily_summary.send_sms")
        mocker.patch(
            "app.escalation.daily_summary.SessionLocal",
            return_value=db_session,
        )

        from app.escalation.daily_summary import send_operational_summary
        send_operational_summary(slot_label="12:00")

        sms_text = mock_send.call_args[0][1]
        # Midday should NOT contain "I dag" (schedule) or "I morgen" (preview)
        assert "I dag:" not in sms_text
        assert "I morgen:" not in sms_text
        # But should contain delta info
        assert "[LISA 12:00]" in sms_text


class TestSummaryLogRecording:
    def test_summary_log_recorded_after_dispatch(self, db_session, mocker):
        """After send_operational_summary, SummaryLog has new row with correct slot_label."""
        mocker.patch("app.escalation.daily_summary.send_sms")
        mocker.patch(
            "app.escalation.daily_summary.SessionLocal",
            return_value=db_session,
        )

        from app.escalation.daily_summary import send_operational_summary
        send_operational_summary(slot_label="12:00")

        logs = db_session.query(SummaryLog).filter_by(slot_label="12:00").all()
        assert len(logs) == 1
        assert logs[0].slot_label == "12:00"

    def test_since_dt_defaults_to_start_of_day_when_no_prior_log(self, db_session, mocker):
        """When no prior SummaryLog for slot, since_dt defaults to start-of-day Copenhagen."""
        mock_send = mocker.patch("app.escalation.daily_summary.send_sms")
        mocker.patch(
            "app.escalation.daily_summary.SessionLocal",
            return_value=db_session,
        )

        # No SummaryLog entries exist
        assert db_session.query(SummaryLog).count() == 0

        from app.escalation.daily_summary import send_operational_summary
        # Should not raise, should work with start-of-day fallback
        send_operational_summary(slot_label="06:00")
        assert mock_send.called


class TestSummaryOwnerPhone:
    def test_summary_sent_to_owner_phone(self, db_session, mocker):
        """Summary SMS is sent to OWNER_PHONE, not to a student phone."""
        mock_send = mocker.patch("app.escalation.daily_summary.send_sms")
        mocker.patch(
            "app.escalation.daily_summary.SessionLocal",
            return_value=db_session,
        )

        from app.escalation.daily_summary import send_operational_summary
        from app.config import OWNER_PHONE
        send_operational_summary(slot_label="12:00")

        # First argument to send_sms should be OWNER_PHONE
        called_to = mock_send.call_args[0][0]
        assert called_to == OWNER_PHONE
        assert called_to != "+4511111111"  # not a student phone


class TestSmsFormat:
    def test_sms_format_has_emoji_and_header(self, db_session, mocker):
        """SMS format includes [LISA HH:MM] header and emoji markers."""
        mock_send = mocker.patch("app.escalation.daily_summary.send_sms")
        mocker.patch(
            "app.escalation.daily_summary.SessionLocal",
            return_value=db_session,
        )

        from app.escalation.daily_summary import send_operational_summary
        send_operational_summary(slot_label="12:00")

        sms_text = mock_send.call_args[0][1]
        assert "[LISA 12:00]" in sms_text
        # Check for at least one emoji marker (bookings, cancellations, or leads)
        has_emoji = any(
            char in sms_text
            for char in ["\u2795", "\u274c", "\U0001f464", "\U0001f4c5"]
        )
        assert has_emoji, f"No emoji found in SMS: {sms_text!r}"
