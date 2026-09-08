"""Tests for cancellation policy engine (CANC-01, CANC-02)."""
from datetime import date, datetime


from app.cancellation.policy import (
    get_cancel_notice_days,
    is_within_cancellation_policy,
    subtract_working_days,
)


class TestSubtractWorkingDays:
    """Tests for working-day arithmetic (D-04: Mon-Fri only)."""

    def test_one_working_day_from_monday(self):
        """Mon 2026-04-20 minus 1 working day = Fri 2026-04-17 (skips weekend)."""
        result = subtract_working_days(date(2026, 4, 20), 1)
        assert result == date(2026, 4, 17)

    def test_four_working_days_from_monday(self):
        """Mon 2026-04-20 minus 4 working days = Tue 2026-04-14 (crosses one weekend)."""
        result = subtract_working_days(date(2026, 4, 20), 4)
        assert result == date(2026, 4, 14)

    def test_six_working_days_crosses_weekend(self):
        """Mon 2026-04-20 minus 6 working days = Fri 2026-04-10 (crosses two weekends)."""
        result = subtract_working_days(date(2026, 4, 20), 6)
        assert result == date(2026, 4, 10)

    def test_one_day_midweek(self):
        """Wed 2026-04-15 minus 1 working day = Tue 2026-04-14 (no weekend crossed)."""
        result = subtract_working_days(date(2026, 4, 15), 1)
        assert result == date(2026, 4, 14)

    def test_zero_days_returns_same(self):
        """Subtracting 0 working days returns the same date."""
        d = date(2026, 4, 15)
        result = subtract_working_days(d, 0)
        assert result == d

    def test_crosses_saturday_and_sunday(self):
        """Mon 2026-04-20 minus 2 working days = Thu 2026-04-16 (weekend not counted)."""
        result = subtract_working_days(date(2026, 4, 20), 2)
        assert result == date(2026, 4, 16)

    def test_from_tuesday(self):
        """Tue 2026-04-21 minus 1 working day = Mon 2026-04-20."""
        result = subtract_working_days(date(2026, 4, 21), 1)
        assert result == date(2026, 4, 20)


class TestIsWithinCancellationPolicy:
    """Tests for deadline boundary checking (D-03: deadline is noon on the deadline date)."""

    # --- Lesson (cancel_notice_days=1) ---

    def test_lesson_within_policy(self):
        """Fri 11:00 for Mon appt: 1 working day before, before noon -> within policy."""
        appt = datetime(2026, 4, 20, 10, 0)   # Monday 10:00
        now = datetime(2026, 4, 17, 11, 0)    # Friday 11:00
        assert is_within_cancellation_policy(appt, "lesson", now=now) is True

    def test_lesson_past_deadline(self):
        """Fri 13:00 for Mon appt: 1 working day before, after noon -> past deadline."""
        appt = datetime(2026, 4, 20, 10, 0)   # Monday 10:00
        now = datetime(2026, 4, 17, 13, 0)    # Friday 13:00
        assert is_within_cancellation_policy(appt, "lesson", now=now) is False

    def test_lesson_exactly_at_noon_deadline(self):
        """Exactly at noon on deadline date -> within policy (now <= deadline)."""
        appt = datetime(2026, 4, 20, 10, 0)   # Monday 10:00
        now = datetime(2026, 4, 17, 12, 0)    # Friday 12:00 exactly
        assert is_within_cancellation_policy(appt, "lesson", now=now) is True

    # --- Manoeuvring (cancel_notice_days=4) ---

    def test_manoeuvring_within_policy(self):
        """Mon 4/13 10:00 for Mon 4/20 appt: 5 working days' notice -> within policy."""
        appt = datetime(2026, 4, 20, 10, 0)   # Monday 4/20
        now = datetime(2026, 4, 13, 10, 0)    # Monday 4/13 -- 5 working days before (Tue,Wed,Thu,Fri,Mon)
        assert is_within_cancellation_policy(appt, "manoeuvring", now=now) is True

    def test_manoeuvring_late(self):
        """Wed 4/15 10:00 for Mon 4/20: only 3 working days' notice -> late."""
        appt = datetime(2026, 4, 20, 10, 0)   # Monday 4/20
        now = datetime(2026, 4, 15, 10, 0)    # Wednesday 4/15 -- only 3 working days (Thu, Fri, Mon)
        assert is_within_cancellation_policy(appt, "manoeuvring", now=now) is False

    # --- Test / driving exam (cancel_notice_days=6) ---

    def test_test_within_policy(self):
        """Mon 4/13 10:00 for Wed 4/22 appt: 7 working days' notice -> within policy."""
        appt = datetime(2026, 4, 22, 10, 0)   # Wednesday 4/22
        now = datetime(2026, 4, 13, 10, 0)    # Monday 4/13 -- 7 working days
        assert is_within_cancellation_policy(appt, "test", now=now) is True

    def test_test_late(self):
        """Wed 4/15 10:00 for Wed 4/22 appt: only 5 working days' notice -> late."""
        appt = datetime(2026, 4, 22, 10, 0)   # Wednesday 4/22
        now = datetime(2026, 4, 15, 10, 0)    # Wednesday 4/15 -- only 5 working days
        assert is_within_cancellation_policy(appt, "test", now=now) is False

    # --- Unknown type falls back to 1 day ---

    def test_unknown_type_defaults_to_one_day(self):
        """Unknown appointment_type falls back to cancel_notice_days=1."""
        appt = datetime(2026, 4, 20, 10, 0)   # Monday
        now = datetime(2026, 4, 17, 10, 0)    # Friday before noon -- 1 working day
        assert is_within_cancellation_policy(appt, "unknown_type", now=now) is True


class TestGetCancelNoticeDays:
    """Tests for cancel_notice_days config lookup."""

    def test_lesson(self):
        assert get_cancel_notice_days("lesson") == 1

    def test_manoeuvring(self):
        assert get_cancel_notice_days("manoeuvring") == 4

    def test_test(self):
        assert get_cancel_notice_days("test") == 6

    def test_unknown_defaults_to_1(self):
        assert get_cancel_notice_days("unknown_type") == 1
