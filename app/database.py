import logging
from pathlib import Path

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

from app.config import DATABASE_URL
from app.models import Base

logger = logging.getLogger(__name__)

# Ensure the directory for SQLite exists (needed for Railway volumes)
if DATABASE_URL.startswith("sqlite:"):
    db_path = DATABASE_URL.replace("sqlite:///", "")
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)

# Rewrite postgresql:// to postgresql+pg8000:// (pure-Python driver, no libpq needed)
_db_url = DATABASE_URL
if _db_url.startswith("postgresql://") or _db_url.startswith("postgres://"):
    _db_url = "postgresql+pg8000" + _db_url[_db_url.index("://"):]

engine = create_engine(_db_url, echo=False)
SessionLocal = sessionmaker(bind=engine)


def _migrate_add_message_sid():
    """Idempotently add messages.message_sid + unique index for existing DBs.

    create_all() does not ALTER existing tables, so DBs that predate this
    column need a one-shot ALTER. Safe to run on every startup; both SQLite
    and Postgres support `CREATE UNIQUE INDEX IF NOT EXISTS`.
    """
    inspector = inspect(engine)
    if "messages" not in inspector.get_table_names():
        return  # init_db() will create the table with the column already present
    columns = {c["name"] for c in inspector.get_columns("messages")}
    if "message_sid" in columns:
        return  # already migrated
    logger.info("Migrating messages table: adding message_sid column + unique index")
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE messages ADD COLUMN message_sid VARCHAR(64)"))
        conn.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS ix_messages_message_sid "
            "ON messages(message_sid)"
        ))


def _migrate_add_booking_slot_unique_index():
    """Idempotently add the partial unique index on bookings(start_dt, end_dt)
    WHERE status='confirmed'.

    create_all() picks this up for fresh DBs (the Index is declared in models.py),
    but existing Railway DBs need the explicit CREATE. If the migration fails —
    most likely because pre-existing duplicate confirmed bookings violate the
    constraint — log loudly and continue; the operator must clean up duplicates
    before this safety net engages.
    """
    inspector = inspect(engine)
    if "bookings" not in inspector.get_table_names():
        return
    indexes = {idx["name"] for idx in inspector.get_indexes("bookings")}
    if "ix_bookings_unique_confirmed_slot" in indexes:
        return
    logger.info("Adding partial unique index on bookings(start_dt, end_dt) WHERE status='confirmed'")
    try:
        with engine.begin() as conn:
            conn.execute(text(
                "CREATE UNIQUE INDEX IF NOT EXISTS ix_bookings_unique_confirmed_slot "
                "ON bookings(start_dt, end_dt) WHERE status='confirmed'"
            ))
    except Exception as exc:
        logger.warning(
            "Could not create ix_bookings_unique_confirmed_slot — likely pre-existing "
            "duplicate confirmed bookings. Resolve duplicates and restart. Error: %s",
            exc,
        )


def init_db():
    Base.metadata.create_all(engine)
    _migrate_add_message_sid()
    _migrate_add_booking_slot_unique_index()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
