import os
from datetime import datetime, timezone, timedelta

import pandas as pd
import streamlit as st
from sqlalchemy import create_engine, func
from sqlalchemy.orm import sessionmaker

# Import SessionLocal from app.database; fall back to a standalone engine
# if the app config cannot be loaded (e.g., Twilio env vars not set).
try:
    from app.database import SessionLocal
except Exception:
    _DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///lisa.db")
    _engine = create_engine(_DATABASE_URL, echo=False)
    SessionLocal = sessionmaker(bind=_engine)

from app.models import Conversation, Message, Escalation, Lead


@st.cache_data(ttl=30)
def get_conversations_df() -> pd.DataFrame:
    """Return all conversations with message count and last message time."""
    db = SessionLocal()
    try:
        rows = (
            db.query(
                Conversation.phone_number,
                Conversation.state,
                func.count(Message.id).label("message_count"),
                func.max(Message.timestamp).label("last_message_at"),
            )
            .outerjoin(Message)
            .group_by(Conversation.id)
            .order_by(func.max(Message.timestamp).desc())
            .all()
        )
        return pd.DataFrame(rows, columns=["phone_number", "state", "message_count", "last_message_at"])
    finally:
        db.close()


@st.cache_data(ttl=30)
def get_escalation_queue_df() -> pd.DataFrame:
    """Return unresolved escalations with student phone, message body, reason, and timestamp."""
    db = SessionLocal()
    try:
        rows = (
            db.query(
                Conversation.phone_number,
                Message.body.label("message_body"),
                Escalation.reason,
                Escalation.timestamp,
            )
            .join(Message, Escalation.message_id == Message.id)
            .join(Conversation, Message.conversation_id == Conversation.id)
            .filter(Escalation.resolved == False)  # noqa: E712
            .order_by(Escalation.timestamp.desc())
            .all()
        )
        return pd.DataFrame(rows, columns=["phone_number", "message_body", "reason", "timestamp"])
    finally:
        db.close()


@st.cache_data(ttl=30)
def get_leads_df() -> pd.DataFrame:
    """Return all leads ordered by creation date descending."""
    db = SessionLocal()
    try:
        rows = (
            db.query(
                Lead.name,
                Lead.phone_number,
                Lead.license_type,
                Lead.desired_start,
                Lead.status,
                Lead.created_at,
            )
            .order_by(Lead.created_at.desc())
            .all()
        )
        return pd.DataFrame(rows, columns=["name", "phone_number", "license_type", "desired_start", "status", "created_at"])
    finally:
        db.close()


@st.cache_data(ttl=30)
def get_message_stats_df() -> pd.DataFrame:
    """Return inbound message volume per day for the last 30 days.

    Aggregates by day in pandas rather than via a SQL date-cast so the query
    behaves identically on SQLite (local) and Postgres (production). SQLite's
    ``CAST(... AS DATE)`` yields a numeric (not a date string), which trips
    SQLAlchemy's date processor — the backend-agnostic path avoids that.
    """
    db = SessionLocal()
    try:
        thirty_days_ago = datetime.now(timezone.utc) - timedelta(days=30)
        rows = (
            db.query(Message.timestamp)
            .filter(
                Message.direction == "inbound",
                Message.timestamp >= thirty_days_ago,
            )
            .all()
        )
    finally:
        db.close()

    timestamps = [r[0] for r in rows if r[0] is not None]
    if not timestamps:
        return pd.DataFrame(columns=["date", "count"])

    days = pd.to_datetime(timestamps, utc=True).date
    daily = pd.Series(days).value_counts().sort_index()
    return pd.DataFrame({"date": daily.index, "count": daily.values})


@st.cache_data(ttl=30)
def get_intent_distribution_df() -> pd.DataFrame:
    """Return count of inbound messages grouped by intent."""
    db = SessionLocal()
    try:
        rows = (
            db.query(
                Message.intent,
                func.count(Message.id).label("count"),
            )
            .filter(
                Message.direction == "inbound",
                Message.intent.isnot(None),
            )
            .group_by(Message.intent)
            .order_by(func.count(Message.id).desc())
            .all()
        )
        return pd.DataFrame(rows, columns=["intent", "count"])
    finally:
        db.close()


def get_summary_metrics() -> dict:
    """Return top-level summary metrics for the dashboard header."""
    db = SessionLocal()
    try:
        today_start = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        )

        total_conversations = db.query(func.count(Conversation.id)).scalar() or 0

        active_today = (
            db.query(func.count(Conversation.id))
            .join(Message)
            .filter(Message.timestamp >= today_start)
            .scalar()
            or 0
        )

        unresolved_escalations = (
            db.query(func.count(Escalation.id))
            .filter(Escalation.resolved == False)  # noqa: E712
            .scalar()
            or 0
        )

        total_leads = db.query(func.count(Lead.id)).scalar() or 0

        return {
            "total_conversations": total_conversations,
            "active_today": active_today,
            "unresolved_escalations": unresolved_escalations,
            "total_leads": total_leads,
        }
    finally:
        db.close()
