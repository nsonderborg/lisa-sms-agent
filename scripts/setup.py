#!/usr/bin/env python3
"""Lisa setup wizard — guided configuration for first-time owners.

Walks through filling in both .env (secrets) and config.yaml (school config)
field by field.  Re-run safe: press Enter to keep existing values.
Use --force to re-enter all fields regardless of current values.

Usage:
    python scripts/setup.py            # guided setup, skip already-set fields
    python scripts/setup.py --force    # re-prompt all fields even if set
"""

import argparse
import os
import sys
import tempfile
from pathlib import Path

import yaml

# Allow importing app modules from project root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ---------------------------------------------------------------------------
# Pure helper functions (unit-tested in tests/test_setup_script.py)
# ---------------------------------------------------------------------------

def parse_env_example(path: str) -> list[tuple[str, str]]:
    """Read .env.example and return an ordered list of (KEY, default) tuples.

    Comment lines and blank lines are skipped.
    """
    result: list[tuple[str, str]] = []
    try:
        with open(path) as f:
            for line in f:
                line = line.rstrip("\n")
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    key, _, default = line.partition("=")
                    key = key.strip()
                    if key:
                        result.append((key, default.strip()))
    except FileNotFoundError:
        pass
    return result


def parse_env_file(path: str) -> dict[str, str]:
    """Read an existing .env file and return a dict of current KEY → value.

    Returns an empty dict if the file does not exist.
    """
    result: dict[str, str] = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.rstrip("\n")
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    key, _, value = line.partition("=")
                    key = key.strip()
                    if key:
                        result[key] = value.strip()
    except FileNotFoundError:
        pass
    return result


def format_env_file(data: dict[str, str]) -> str:
    """Produce a KEY=VALUE string for writing to a .env file."""
    return "".join(f"{k}={v}\n" for k, v in data.items())


def load_config_yaml(path: str) -> dict:
    """Load config.yaml and return the parsed dict."""
    with open(path) as f:
        return yaml.safe_load(f) or {}


def save_config_yaml(path: str, data: dict) -> None:
    """Write a dict back to config.yaml."""
    with open(path, "w") as f:
        yaml.dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)


# ---------------------------------------------------------------------------
# Per-variable descriptions shown in prompts
# ---------------------------------------------------------------------------

_ENV_DESCRIPTIONS: dict[str, str] = {
    "TWILIO_ACCOUNT_SID": "Twilio Account SID (fra console.twilio.com)",
    "TWILIO_AUTH_TOKEN": "Twilio Auth Token (fra console.twilio.com)",
    "TWILIO_PHONE_NUMBER": "Twilio-telefonnummer i E.164-format (eks. +4512345678)",
    "ANTHROPIC_API_KEY": "Anthropic API-nøgle (fra console.anthropic.com)",
    "OWNER_PHONE": "Dit personlige telefonnummer i E.164-format (eks. +4512345678)",
    "DATABASE_URL": "Database-URL (tryk Enter for standard: sqlite:///lisa.db)",
    "ICAL_URL": "Apple Calendar iCal-URL (valgfri, tryk Enter for at springe over)",
    "GOOGLE_SERVICE_ACCOUNT_JSON": "Google-tjenestekonto-legitimationsoplysninger JSON-streng",
}

_CONFIG_FIELDS: list[tuple[str, str]] = [
    ("school_name",       "Køreskoles navn (PÅKRÆVET) — eks. Køreskole Jensen"),
    ("address",           "Køreskoles adresse — eks. Vestergade 12"),
    ("city",              "By — eks. København"),
    ("theory_schedule",   "Teoriundervisningsplan — eks. Mandag og onsdag kl. 17:00-19:00"),
    ("pricing",           "Prisoversigt — eks. Kørekort kategori B: 14.500 kr"),
    ("gcal_calendar_id",  "Google Calendar ID (PÅKRÆVET) — Google Calendar > Indstillinger > Kalender-id"),
]


# ---------------------------------------------------------------------------
# Interactive helpers
# ---------------------------------------------------------------------------

def _prompt(label: str, current: str, force: bool) -> str:
    """Prompt the user for a value.  Returns current unchanged if non-empty and not forced."""
    if current and not force:
        print(f"  {label}: [allerede sat, beholder]")
        return current
    display = f" [{current}]" if current else ""
    value = input(f"  {label}{display}: ").strip()
    return value if value else current


# ---------------------------------------------------------------------------
# Main setup flow
# ---------------------------------------------------------------------------

def run_setup(force: bool = False, env_path: str = ".env",
              example_path: str = ".env.example",
              config_path: str | None = None) -> None:
    """Run the interactive setup wizard.

    ``config_path`` defaults to whatever ``CONFIG_PATH`` selects, matching
    ``app/config.py``. Without that, the wizard always wrote the repo-root
    ``config.yaml`` — so running it to onboard a second school would silently
    overwrite the first school's config, with no prompt and no backup.
    Resolution mirrors app/config.py: relative against the repo root, absolute
    honoured as-is.
    """
    project_root = Path(__file__).resolve().parent.parent

    if config_path is None:
        config_path = os.environ.get("CONFIG_PATH", "").strip() or "config.yaml"

    env_path = str(project_root / env_path)
    example_path = str(project_root / example_path)
    config_path = str(project_root / config_path)

    # ------------------------------------------------------------------
    # Section 1: Environment Variables (.env)
    # ------------------------------------------------------------------
    print("\n=== Del 1: Miljøvariabler (.env) ===\n")

    env_vars = parse_env_example(example_path)
    if not env_vars:
        print("  ⚠  .env.example ikke fundet — springer miljøvariabler over.")
    else:
        current_env = parse_env_file(env_path)
        new_env: dict[str, str] = {}

        for key, default in env_vars:
            current = current_env.get(key, default)
            description = _ENV_DESCRIPTIONS.get(key, key)
            new_env[key] = _prompt(description, current, force)

        tmp_fd, tmp_path = tempfile.mkstemp(dir=str(project_root), prefix=".env.tmp")
        try:
            with os.fdopen(tmp_fd, "w") as f:
                f.write(format_env_file(new_env))
                f.write("\n")
            os.replace(tmp_path, env_path)
        except Exception:
            os.unlink(tmp_path)
            raise
        print("\n  .env skrevet korrekt.")

    # ------------------------------------------------------------------
    # Section 2: School Configuration (config.yaml)
    # ------------------------------------------------------------------
    print("\n=== Del 2: Køreskole-konfiguration (config.yaml) ===\n")

    try:
        config_data = load_config_yaml(config_path)
    except FileNotFoundError:
        print("  ⚠  config.yaml ikke fundet — kan ikke konfigurere skoleindstillinger.")
        config_data = {}

    if config_data:
        for field, description in _CONFIG_FIELDS:
            current = str(config_data.get(field, "") or "")
            config_data[field] = _prompt(description, current, force)

        save_config_yaml(config_path, config_data)
        print("\n  config.yaml skrevet korrekt.")

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------
    print()

    os.environ["LISA_SKIP_VALIDATION"] = "1"

    # Reload env from the .env we just wrote
    try:
        from dotenv import load_dotenv
        load_dotenv(dotenv_path=env_path, override=True)
    except ImportError:
        pass

    # Import validate_config freshly (module may already be cached)
    try:
        # Force reload so updated env vars are picked up
        import importlib
        import app.config as _cfg_mod
        importlib.reload(_cfg_mod)
        errors = _cfg_mod.validate_config()
    except (SystemExit, KeyboardInterrupt):
        raise
    except Exception as exc:
        print(f"⚠  Kunne ikke køre validering: {exc}")
        return

    if not errors:
        print("✅ Opsætning gennemført! Alle påkrævede felter er konfigureret.")
        print("   Kør: uvicorn app.main:app")
    else:
        for err in errors:
            print(f"  ❌ Config error: {err}")
        print("\n⚠  Opsætning ufuldstændig — ret fejlene ovenfor og kør igen: python scripts/setup.py")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Lisa setup wizard — configure .env and config.yaml interactively."
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-prompt all fields even if they already have values.",
    )
    args = parser.parse_args()
    run_setup(force=args.force)
