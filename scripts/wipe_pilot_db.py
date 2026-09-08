"""Pre-pilot database wipe — clears accumulated TEST data so the first real
pilot student starts on a blank slate.

SAFE BY DESIGN:
  * Touches ONLY the 6 content tables in WIPE_TABLES. It NEVER touches
    ``calendar_cache`` (a derived cache that auto-refills from the live
    calendars on the next poll), and NEVER touches schema, code, or config.
  * This script imports NO application code — it only knows table names.
  * Refuses to run if a real pilot-student number (an allowlisted number that
    is NOT the owner) already has a conversation, so it cannot nuke real data
    once the pilot is live. Override only with an explicit --force.
  * Nothing is deleted until you type WIPE at the interactive prompt.

Run against production via Railway (so it uses DATABASE_PUBLIC_URL):

    railway run python scripts/wipe_pilot_db.py

…or pass an explicit URL:

    python scripts/wipe_pilot_db.py --database-url postgresql://...
"""
import argparse
import os
import sys

from sqlalchemy import create_engine, text

# The ONLY tables this script will ever modify. Ordered children-first so the
# fallback DELETE path respects foreign keys. calendar_cache is deliberately
# absent — it is a derived cache, refilled by the background calendar poll.
WIPE_TABLES = [
    "escalations",   # FK -> messages
    "messages",      # FK -> conversations
    "bookings",      # FK -> conversations
    "leads",
    "summary_log",
    "conversations",
]
KEEP_TABLES = ["calendar_cache"]


def _resolve_url(arg_url):
    url = arg_url or os.environ.get("DATABASE_PUBLIC_URL") or os.environ.get("DATABASE_URL")
    if not url:
        sys.exit(
            "No database URL found. Pass --database-url, or run via `railway run` "
            "(which injects DATABASE_PUBLIC_URL / DATABASE_URL)."
        )
    return url


def _protected_numbers():
    """Allowlisted pilot numbers that are NOT the owner — i.e. real students."""
    allow = {p.strip() for p in os.environ.get("BETA_PHONE_ALLOWLIST", "").split(",") if p.strip()}
    allow.discard(os.environ.get("OWNER_PHONE", "").strip())
    return allow


def main():
    parser = argparse.ArgumentParser(description="Pre-pilot test-data wipe (safe, guarded).")
    parser.add_argument("--database-url", help="Override DB URL (else DATABASE_PUBLIC_URL/DATABASE_URL).")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Proceed even if a pilot-student number already has data (DANGEROUS).",
    )
    args = parser.parse_args()

    url = _resolve_url(args.database_url)
    engine = create_engine(url)
    dialect = engine.dialect.name

    # --- Inspect (read-only) ---
    counts = {}
    with engine.connect() as conn:
        host = url.split("@")[-1].split("/")[0] if "@" in url else "(local)"
        print(f"Connected: {dialect} @ {host}\n")
        print("Current row counts:")
        for t in WIPE_TABLES + KEEP_TABLES:
            try:
                counts[t] = conn.execute(text(f"SELECT count(*) FROM {t}")).scalar()
            except Exception as e:  # noqa: BLE001
                counts[t] = f"ERR ({type(e).__name__})"
            tag = "   <- KEPT (not wiped)" if t in KEEP_TABLES else ""
            print(f"  {t:16} {counts[t]}{tag}")

        # Guard: refuse if a real pilot student already has a conversation.
        protected = _protected_numbers()
        convo_numbers = {r[0] for r in conn.execute(text("SELECT DISTINCT phone_number FROM conversations"))}
        hit = sorted(protected & convo_numbers)

    if hit and not args.force:
        print("\nABORT — these protected (pilot-student) numbers already have data:")
        for h in hit:
            print(f"   {h}")
        print("Refusing to wipe real pilot data. Re-run with --force only if you are certain.")
        return 2

    wipe_total = sum(v for v in (counts[t] for t in WIPE_TABLES) if isinstance(v, int))
    print(f"\nAbout to DELETE every row from: {', '.join(WIPE_TABLES)}")
    print(f"(~{wipe_total} rows). {', '.join(KEEP_TABLES)} will be left intact.")
    if input("Type WIPE to proceed (anything else aborts): ").strip() != "WIPE":
        print("Aborted. Nothing was changed.")
        return 1

    # --- Wipe (transactional) ---
    with engine.begin() as conn:
        if dialect == "postgresql":
            conn.execute(text("TRUNCATE TABLE " + ", ".join(WIPE_TABLES) + " RESTART IDENTITY CASCADE"))
        else:
            for t in WIPE_TABLES:  # already children-first
                conn.execute(text(f"DELETE FROM {t}"))

    with engine.connect() as conn:
        print("\nDone. Post-wipe row counts:")
        for t in WIPE_TABLES + KEEP_TABLES:
            n = conn.execute(text(f"SELECT count(*) FROM {t}")).scalar()
            print(f"  {t:16} {n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
