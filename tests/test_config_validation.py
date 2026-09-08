"""
Tests for app/config.py startup validation.

validate_config() returns a list of human-readable error strings.
Empty list means config is valid.

All tests import with LISA_SKIP_VALIDATION=1 to avoid module-level sys.exit().
"""
import os

# Ensure LISA_SKIP_VALIDATION is set before any app.config import
os.environ["LISA_SKIP_VALIDATION"] = "1"

# Also ensure minimum env vars so config.py doesn't crash during import
os.environ.setdefault("TWILIO_ACCOUNT_SID", "ACtest00000000000000000000000000000")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test_auth_token_placeholder")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "+15005550006")
os.environ.setdefault("ANTHROPIC_API_KEY", "test_anthropic_key")
os.environ.setdefault("OWNER_PHONE", "+4500000000")
os.environ.setdefault("GOOGLE_SERVICE_ACCOUNT_JSON", '{"type":"service_account"}')

import pytest


def _call_validate(env_overrides: dict, config_overrides: dict) -> list[str]:
    """
    Call validate_config() with controlled env and config values.

    Temporarily patches os.environ, the SCHOOL_NAME module-level variable, and
    the SCHOOL_CONFIG dict in app.config to isolate each test. NB: the
    calendar-backend checks in validate_config() read the SCHOOL_CONFIG dict
    directly (calendar_backend, gcal_calendar_id, caldav_calendar_name) — the
    GCAL_CALENDAR_ID module var is NOT consulted, so it is not patched here.
    """
    import app.config as cfg

    # Apply env overrides temporarily
    original_env = {}
    for k, v in env_overrides.items():
        original_env[k] = os.environ.get(k)
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v

    # Apply config overrides temporarily
    original_school_name = cfg.SCHOOL_NAME
    _MISSING = object()
    original_school_config = {}

    if "school_name" in config_overrides:
        cfg.SCHOOL_NAME = config_overrides["school_name"]
    for key in ("gcal_calendar_id", "calendar_backend", "caldav_calendar_name"):
        if key in config_overrides:
            original_school_config[key] = cfg.SCHOOL_CONFIG.get(key, _MISSING)
            cfg.SCHOOL_CONFIG[key] = config_overrides[key]

    try:
        errors = cfg.validate_config()
    finally:
        # Restore env
        for k, original_val in original_env.items():
            if original_val is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = original_val

        # Restore config module vars + SCHOOL_CONFIG dict entries
        cfg.SCHOOL_NAME = original_school_name
        for key, original_val in original_school_config.items():
            if original_val is _MISSING:
                cfg.SCHOOL_CONFIG.pop(key, None)
            else:
                cfg.SCHOOL_CONFIG[key] = original_val

    return errors


# ---------------------------------------------------------------------------
# Test 1: Missing required env var produces error naming the var
# ---------------------------------------------------------------------------
def test_missing_twilio_account_sid_produces_error():
    """Missing TWILIO_ACCOUNT_SID must produce an error containing the var name."""
    errors = _call_validate(
        env_overrides={"TWILIO_ACCOUNT_SID": ""},
        config_overrides={"school_name": "Test Skole", "gcal_calendar_id": "cal123@group.calendar.google.com"},
    )
    assert any("TWILIO_ACCOUNT_SID" in e for e in errors), (
        f"Expected error mentioning TWILIO_ACCOUNT_SID, got: {errors}"
    )


# ---------------------------------------------------------------------------
# Test 2: Empty required config field school_name produces named error
# ---------------------------------------------------------------------------
def test_empty_school_name_produces_error():
    """Empty school_name in config.yaml must produce an error containing 'school_name'."""
    errors = _call_validate(
        env_overrides={},
        config_overrides={"school_name": "", "gcal_calendar_id": "cal123@group.calendar.google.com"},
    )
    assert any("school_name" in e for e in errors), (
        f"Expected error mentioning school_name, got: {errors}"
    )


# ---------------------------------------------------------------------------
# Test 3: Empty required config field gcal_calendar_id produces named error
# ---------------------------------------------------------------------------
def test_empty_gcal_calendar_id_produces_error():
    """Empty gcal_calendar_id must produce an error — but only on the google backend.

    Since the CalDAV migration this check is conditional on
    calendar_backend == "google", so the backend is forced here.
    """
    errors = _call_validate(
        env_overrides={},
        config_overrides={
            "school_name": "Test Skole",
            "calendar_backend": "google",
            "gcal_calendar_id": "",
        },
    )
    assert any("gcal_calendar_id" in e for e in errors), (
        f"Expected error mentioning gcal_calendar_id, got: {errors}"
    )


# ---------------------------------------------------------------------------
# Test 4: All errors collected together (fail-all, not fail-first)
# ---------------------------------------------------------------------------
def test_multiple_missing_fields_all_reported():
    """Two missing fields must both appear in the error list."""
    errors = _call_validate(
        env_overrides={"TWILIO_ACCOUNT_SID": "", "ANTHROPIC_API_KEY": ""},
        config_overrides={"school_name": "Test Skole", "gcal_calendar_id": "cal123"},
    )
    has_twilio_error = any("TWILIO_ACCOUNT_SID" in e for e in errors)
    has_anthropic_error = any("ANTHROPIC_API_KEY" in e for e in errors)
    assert has_twilio_error and has_anthropic_error, (
        f"Expected both TWILIO_ACCOUNT_SID and ANTHROPIC_API_KEY errors, got: {errors}"
    )


# ---------------------------------------------------------------------------
# Test 5: Valid config produces no errors
# ---------------------------------------------------------------------------
def test_valid_config_produces_no_errors():
    """All required fields set and non-empty must produce zero errors."""
    errors = _call_validate(
        env_overrides={
            "TWILIO_ACCOUNT_SID": "ACtest00000000000000000000000000000",
            "TWILIO_AUTH_TOKEN": "valid_token",
            "TWILIO_PHONE_NUMBER": "+15005550006",
            "ANTHROPIC_API_KEY": "sk-ant-test",
            "OWNER_PHONE": "+4512345678",
            "GOOGLE_SERVICE_ACCOUNT_JSON": '{"type":"service_account"}',
        },
        config_overrides={"school_name": "Køreskole Jensen", "gcal_calendar_id": "abc@group.calendar.google.com"},
    )
    assert errors == [], f"Expected no errors for valid config, got: {errors}"


# ---------------------------------------------------------------------------
# Test 6: Optional fields can be empty without error
# ---------------------------------------------------------------------------
def test_optional_fields_empty_is_ok():
    """theory_schedule, pricing, faq being empty should NOT produce errors."""
    import app.config as cfg

    # Temporarily blank out optional fields on the module
    original_theory = cfg.THEORY_SCHEDULE
    original_pricing = cfg.PRICING
    original_faq = cfg.FAQ

    cfg.THEORY_SCHEDULE = ""
    cfg.PRICING = ""
    cfg.FAQ = {}

    try:
        errors = _call_validate(
            env_overrides={
                "TWILIO_ACCOUNT_SID": "ACtest",
                "TWILIO_AUTH_TOKEN": "tok",
                "TWILIO_PHONE_NUMBER": "+15005550006",
                "ANTHROPIC_API_KEY": "sk-ant-test",
                "OWNER_PHONE": "+4512345678",
                "GOOGLE_SERVICE_ACCOUNT_JSON": '{"type":"service_account"}',
            },
            config_overrides={"school_name": "Køreskole Jensen", "gcal_calendar_id": "abc@group.calendar.google.com"},
        )
    finally:
        cfg.THEORY_SCHEDULE = original_theory
        cfg.PRICING = original_pricing
        cfg.FAQ = original_faq

    assert errors == [], f"Optional empty fields should not produce errors, got: {errors}"


# ---------------------------------------------------------------------------
# Test 7: Error messages do NOT contain actual secret values
# ---------------------------------------------------------------------------
def test_error_messages_do_not_contain_secret_values():
    """Error messages must name the field, not echo back the secret value."""
    secret_value = "supersecret_token_abc123"
    # Place secret_value in env so validate_config *could* echo it back in an
    # error message if it were careless.  We also leave another required field
    # empty so at least one validation error is generated.
    errors = _call_validate(
        env_overrides={
            "TWILIO_AUTH_TOKEN": secret_value,
            "TWILIO_ACCOUNT_SID": "",
        },
        config_overrides={"school_name": "Test Skole", "gcal_calendar_id": "cal123"},
    )
    # The secret value must NOT appear in any error message
    for err in errors:
        assert secret_value not in err, (
            f"Error message leaked secret value: {err}"
        )
    # At least one error must be present (TWILIO_ACCOUNT_SID is empty)
    assert len(errors) > 0, "Expected validation errors for missing TWILIO_ACCOUNT_SID"


# ---------------------------------------------------------------------------
# Test 8: validate_config is importable as a function (not auto-exiting)
# ---------------------------------------------------------------------------
def test_validate_config_is_importable_as_function():
    """validate_config must be importable and callable as a plain function."""
    from app.config import validate_config
    assert callable(validate_config)


# ---------------------------------------------------------------------------
# Test 9: _enforce_config exists and calls sys.exit on errors
# ---------------------------------------------------------------------------
def test_enforce_config_exits_on_errors(monkeypatch):
    """_enforce_config() must call sys.exit(1) when there are errors."""
    import app.config as cfg

    # Patch validate_config to return errors
    monkeypatch.setattr(cfg, "SCHOOL_NAME", "")
    monkeypatch.setattr(cfg, "GCAL_CALENDAR_ID", "")

    with pytest.raises(SystemExit) as exc_info:
        cfg._enforce_config()

    assert exc_info.value.code == 1
