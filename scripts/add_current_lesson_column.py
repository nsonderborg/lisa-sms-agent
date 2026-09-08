"""999.37 pilot seed — add the leads.current_lesson column to an existing DB.

The repo has no Alembic; the schema is created via create_all in init_db.py, which
only adds NEW tables, never new columns to existing ones. Production Postgres
already has a `leads` table, so the new `Lead.current_lesson` column must be added
with this one-off, idempotent migration.

Run against production via Railway (so it uses DATABASE_PUBLIC_URL — the internal
hostname is not reachable from a local `railway run`):

    railway run python scripts/add_current_lesson_column.py

…or pass an explicit URL:

    python scripts/add_current_lesson_column.py --database-url postgresql://...

Idempotent: inspects existing columns first and is a no-op if the column already
exists. Dialect-agnostic (Postgres prod / SQLite local). Imports NO app code.
"""

import argparse
import os
import sys

from sqlalchemy import create_engine, inspect, text


def _resolve_url(arg_url: str | None) -> str:
    url = arg_url or os.environ.get("DATABASE_PUBLIC_URL") or os.environ.get("DATABASE_URL")
    if not url:
        print(
            "No database URL. Pass --database-url or run via `railway run` "
            "(which injects DATABASE_PUBLIC_URL / DATABASE_URL).",
            file=sys.stderr,
        )
        sys.exit(2)
    return url


def main() -> int:
    parser = argparse.ArgumentParser(description="999.37 — add leads.current_lesson (idempotent).")
    parser.add_argument("--database-url", help="Override DB URL (else DATABASE_PUBLIC_URL/DATABASE_URL).")
    args = parser.parse_args()

    engine = create_engine(_resolve_url(args.database_url))
    inspector = inspect(engine)

    if "leads" not in inspector.get_table_names():
        print("leads table does not exist yet — run scripts/init_db.py first.")
        return 1

    columns = {col["name"] for col in inspector.get_columns("leads")}
    if "current_lesson" in columns:
        print("leads.current_lesson already exists — nothing to do.")
        return 0

    print(f"Adding leads.current_lesson against {engine.dialect.name} ...")
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE leads ADD COLUMN current_lesson INTEGER"))
    print("Done. leads.current_lesson added (nullable INTEGER).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
