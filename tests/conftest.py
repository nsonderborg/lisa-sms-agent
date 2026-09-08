"""
Shared test fixtures for the Lisa test suite.

Provides:
  - db_session: SQLite in-memory session with schema pre-created
  - client: FastAPI TestClient for webhook endpoint tests
  - mock_send_sms: Prevents real Twilio calls during tests
"""
import os

# Set required env vars before any app imports
# Skip import-time config enforcement (app.config._enforce_config → sys.exit) so the
# suite is self-contained on a clean machine / CI with no .env. validate_config() is
# still exercised directly by test_config_validation.py, and prod never loads conftest.
os.environ.setdefault("LISA_SKIP_VALIDATION", "1")
os.environ.setdefault("TWILIO_ACCOUNT_SID", "ACtest00000000000000000000000000000")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test_auth_token_placeholder")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "+15005550006")
os.environ.setdefault("ANTHROPIC_API_KEY", "test_anthropic_key")
os.environ.setdefault("OWNER_PHONE", "+4500000000")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from datetime import datetime

import pytest
from unittest.mock import MagicMock
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models import Base


@pytest.fixture(autouse=True)
def _freeze_confirm_guard_clock(monkeypatch):
    """Freeze the confirm-time past-slot guards' clock for the whole suite.

    The booking + reschedule confirm paths now re-validate that the offered slot
    is still in the future, reading wall-clock now via the patchable
    `_now_local()` seam. Much of the suite mocks slot finders with fixed
    historical dates (May 2026); without this freeze those time-travelling
    fixtures would be rejected by the new guard. Freezing to 2026-01-01 (before
    every mocked slot) is a no-op for all existing assertions. Tests that
    exercise the guard itself re-patch `_now_local` explicitly (last patch wins).

    Scope, precisely: this freezes ONLY the two handler seams. `slot_engine`
    has its own `_now_local` (feeding the front-edge filter, and now the
    slot_finder day-walk anchor derived from it) which is deliberately left on
    the real clock here — so suite-wide the handlers sit in January 2026 while
    the slot subsystem runs in real time. That split is intentional but easy to
    trip over: a test that needs the slot subsystem frozen must patch
    `slot_engine._now_local` itself.
    """
    import app.booking.handler as _bh
    import app.cancellation.handler as _ch

    frozen = datetime(2026, 1, 1, 0, 0)
    monkeypatch.setattr(_bh, "_now_local", lambda: frozen, raising=False)
    monkeypatch.setattr(_ch, "_now_local", lambda: frozen, raising=False)


@pytest.fixture
def db_session():
    """SQLite in-memory database session with all tables created.

    Uses StaticPool so all connections share the same in-memory database.
    This ensures the session is usable across threads (FastAPI TestClient
    runs request handlers in a separate thread from the test).
    """
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


@pytest.fixture
def client(db_session):
    """FastAPI TestClient with DB dependency overridden to use the test session."""
    from fastapi.testclient import TestClient
    from app.main import app
    from app.database import get_db

    def override_get_db():
        try:
            yield db_session
        finally:
            pass

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture
def mock_send_sms(mocker):
    """Prevent real Twilio calls during tests."""
    mocker.patch(
        "app.sms.sender.client.messages.create",
        return_value=MagicMock(sid="TEST_SID"),
    )
    mocker.patch(
        "app.conversation.manager.send_sms",
        return_value="TEST_SID",
    )
