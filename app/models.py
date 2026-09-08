from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    pass


class Conversation(Base):
    __tablename__ = "conversations"

    id = Column(Integer, primary_key=True)
    phone_number = Column(String(20), unique=True, nullable=False, index=True)
    state = Column(String(50), nullable=False, default="idle")
    context_json = Column(Text, default="{}")
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    messages = relationship("Message", back_populates="conversation")


class Message(Base):
    __tablename__ = "messages"

    id = Column(Integer, primary_key=True)
    conversation_id = Column(Integer, ForeignKey("conversations.id"), nullable=False)
    direction = Column(String(10), nullable=False)  # "inbound" or "outbound"
    body = Column(Text, nullable=False)
    intent = Column(String(50))
    confidence = Column(Float)
    language = Column(String(5))
    timestamp = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    # Twilio MessageSid for inbound-message idempotency. NULL on outbound and
    # on inbound rows that predate this column. Multiple NULLs are fine —
    # both SQLite and Postgres treat NULLs as distinct in UNIQUE indexes.
    message_sid = Column(String(64), nullable=True, unique=True, index=True)

    conversation = relationship("Conversation", back_populates="messages")
    escalation = relationship("Escalation", back_populates="message", uselist=False)


class Escalation(Base):
    __tablename__ = "escalations"

    id = Column(Integer, primary_key=True)
    message_id = Column(Integer, ForeignKey("messages.id"), nullable=False)
    reason = Column(String(100), nullable=False)
    resolved = Column(Boolean, default=False)
    timestamp = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    message = relationship("Message", back_populates="escalation")


class Lead(Base):
    __tablename__ = "leads"

    id = Column(Integer, primary_key=True)
    phone_number = Column(String(20), nullable=False)
    name = Column(String(100))
    license_type = Column(String(10))
    desired_start = Column(String(50))
    # 999.37 pilot seed — student's self-reported current lektionsplan number,
    # captured once at first-booking onboarding. Nullable: brand-new column on a
    # one-time seed; "ved ikke" / pre-feature students stay NULL. A nudge signal
    # for the owner, NOT live truth (no auto-increment per booking).
    current_lesson = Column(Integer, nullable=True)
    status = Column(String(20), default="new")  # new, contacted, converted
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class Booking(Base):
    __tablename__ = "bookings"

    id = Column(Integer, primary_key=True)
    phone_number = Column(String(20), nullable=False, index=True)
    conversation_id = Column(Integer, ForeignKey("conversations.id"), nullable=False)
    appointment_type = Column(String(50), nullable=False)  # "lesson" | "manoeuvring" | "test"
    start_dt = Column(DateTime, nullable=False)            # naive UTC
    end_dt = Column(DateTime, nullable=False)              # naive UTC
    gcal_event_id = Column(String(200))                    # None until calendar write succeeds
    status = Column(String(20), default="pending")         # pending | confirmed | cancelled | reschedule_pending
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    cancelled_at = Column(DateTime, nullable=True)  # set when status -> cancelled

    conversation = relationship("Conversation")


# Partial unique index — at most one CONFIRMED booking per (start_dt, end_dt).
# Closes the cross-student same-slot race in _confirm_booking and reschedule
# confirm: two students offered the same slot, both passing the read-then-write
# conflict check before either commits, will now have the second insert
# rejected by the DB with IntegrityError. Cancelled / reschedule_pending rows
# are excluded so historical data and in-flight reschedules don't conflict.
Index(
    "ix_bookings_unique_confirmed_slot",
    Booking.start_dt,
    Booking.end_dt,
    unique=True,
    sqlite_where=text("status='confirmed'"),
    postgresql_where=text("status='confirmed'"),
)


class CalendarCache(Base):
    __tablename__ = "calendar_cache"

    id = Column(Integer, primary_key=True)
    event_id = Column(String(200), unique=True, nullable=False, index=True)
    summary = Column(Text)
    start_dt = Column(DateTime, nullable=False)
    end_dt = Column(DateTime, nullable=False)
    fetched_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class SummaryLog(Base):
    __tablename__ = "summary_log"

    id = Column(Integer, primary_key=True)
    slot_label = Column(String(10), nullable=False)  # "06:00", "12:00", "17:00"
    sent_at = Column(DateTime, nullable=False)        # UTC
