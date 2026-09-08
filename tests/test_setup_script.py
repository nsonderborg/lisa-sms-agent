"""Tests for scripts/setup.py helper functions.

Tests cover pure helper functions only — not interactive prompts.
LISA_SKIP_VALIDATION=1 must be set before importing any app modules.
"""
import os
import sys
from pathlib import Path


# Prevent config.py validation from running during import
os.environ["LISA_SKIP_VALIDATION"] = "1"

# Add project root to path so scripts/ is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.setup import (
    format_env_file,
    load_config_yaml,
    parse_env_example,
    parse_env_file,
    save_config_yaml,
)


SAMPLE_ENV_EXAMPLE = """\
# Comment line — should be ignored
TWILIO_ACCOUNT_SID=
TWILIO_AUTH_TOKEN=
# Another comment
TWILIO_PHONE_NUMBER=
DATABASE_URL=sqlite:///lisa.db
ICAL_URL=
"""

SAMPLE_ENV_FILE = """\
TWILIO_ACCOUNT_SID=AC123
TWILIO_AUTH_TOKEN=secret
TWILIO_PHONE_NUMBER=
DATABASE_URL=sqlite:///lisa.db
ICAL_URL=
"""


class TestParseEnvExample:
    def test_returns_ordered_list_of_tuples(self, tmp_path):
        example = tmp_path / ".env.example"
        example.write_text(SAMPLE_ENV_EXAMPLE)
        result = parse_env_example(str(example))
        keys = [k for k, _ in result]
        assert keys == [
            "TWILIO_ACCOUNT_SID",
            "TWILIO_AUTH_TOKEN",
            "TWILIO_PHONE_NUMBER",
            "DATABASE_URL",
            "ICAL_URL",
        ]

    def test_captures_default_values(self, tmp_path):
        example = tmp_path / ".env.example"
        example.write_text(SAMPLE_ENV_EXAMPLE)
        result = parse_env_example(str(example))
        defaults = dict(result)
        assert defaults["DATABASE_URL"] == "sqlite:///lisa.db"
        assert defaults["TWILIO_ACCOUNT_SID"] == ""

    def test_skips_comment_lines(self, tmp_path):
        example = tmp_path / ".env.example"
        example.write_text(SAMPLE_ENV_EXAMPLE)
        result = parse_env_example(str(example))
        keys = [k for k, _ in result]
        assert all(not k.startswith("#") for k in keys)

    def test_empty_file_returns_empty_list(self, tmp_path):
        example = tmp_path / ".env.example"
        example.write_text("")
        result = parse_env_example(str(example))
        assert result == []


class TestParseEnvFile:
    def test_returns_dict_of_current_values(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text(SAMPLE_ENV_FILE)
        result = parse_env_file(str(env))
        assert result["TWILIO_ACCOUNT_SID"] == "AC123"
        assert result["TWILIO_AUTH_TOKEN"] == "secret"

    def test_empty_value_returns_empty_string(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text(SAMPLE_ENV_FILE)
        result = parse_env_file(str(env))
        assert result["TWILIO_PHONE_NUMBER"] == ""

    def test_missing_file_returns_empty_dict(self, tmp_path):
        result = parse_env_file(str(tmp_path / "nonexistent.env"))
        assert result == {}


class TestFormatEnvFile:
    def test_produces_key_value_lines(self):
        data = {"KEY_A": "value1", "KEY_B": ""}
        result = format_env_file(data)
        assert "KEY_A=value1\n" in result
        assert "KEY_B=\n" in result

    def test_preserves_insertion_order(self):
        data = {"FIRST": "1", "SECOND": "2", "THIRD": "3"}
        result = format_env_file(data)
        lines = [ln for ln in result.splitlines() if "=" in ln]
        assert lines == ["FIRST=1", "SECOND=2", "THIRD=3"]

    def test_no_extra_whitespace(self):
        result = format_env_file({"KEY": "val"})
        assert result.strip() == "KEY=val"


class TestLoadSaveConfigYaml:
    def test_load_returns_dict(self, tmp_path):
        cfg = tmp_path / "config.yaml"
        cfg.write_text("school_name: Jensen\ngcal_calendar_id: abc123\n")
        result = load_config_yaml(str(cfg))
        assert result["school_name"] == "Jensen"
        assert result["gcal_calendar_id"] == "abc123"

    def test_save_then_load_round_trips(self, tmp_path):
        cfg = tmp_path / "config.yaml"
        cfg.write_text("school_name: \ngcal_calendar_id: \n")
        data = load_config_yaml(str(cfg))
        data["school_name"] = "Køreskole Test"
        save_config_yaml(str(cfg), data)
        reloaded = load_config_yaml(str(cfg))
        assert reloaded["school_name"] == "Køreskole Test"

    def test_save_preserves_other_keys(self, tmp_path):
        cfg = tmp_path / "config.yaml"
        cfg.write_text("school_name: Old\npricing: 14000\n")
        data = load_config_yaml(str(cfg))
        data["school_name"] = "New"
        save_config_yaml(str(cfg), data)
        reloaded = load_config_yaml(str(cfg))
        assert reloaded["pricing"] == 14000
        assert reloaded["school_name"] == "New"
