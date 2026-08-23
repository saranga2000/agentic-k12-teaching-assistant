"""Promote a source's printed page number to its primary identity schema.

For a source whose current schema already has confirmed mappings (e.g. Summer
Bridge's Day+Section, before this: docs/ROADMAP.md's M3.7 finding that Section is
never printed on an exercise page, only Day), this:

1. Saves a new schema version with one component, "page_number".
2. Backfills confirmed page_identities rows for it from the current version's own
   page_number column -- no re-scanning, no re-typing (k12ta.store.page_identities.
   backfill_page_number_schema).

The old schema stays exactly as it was, one version back, and
k12ta.grading.page_identity.resolve_with_schema_history still falls back to it
(Day-only auto-resolve via resolve_partial) when a photo's page number isn't
legible. Safe to re-run: both the schema save and the backfill are additive/upsert.

    python scripts/add_page_number_schema.py dev-jahnvi summer_bridge
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime

from k12ta.config import Settings
from k12ta.store import db, migrate, page_identities, page_identity_schemas


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("student_id")
    parser.add_argument("source_id")
    args = parser.parse_args()

    settings = Settings.from_env()
    conn = db.connect(str(settings.data_dir / "k12ta.db"))
    migrate.apply_migrations(conn)

    from_version = page_identity_schemas.get_current_version(conn, args.student_id, args.source_id)
    if from_version == 0:
        raise SystemExit(f"{args.source_id} has no identity schema yet -- nothing to promote from")

    new_version = page_identity_schemas.save_new_schema(
        conn, args.student_id, args.source_id, [("page_number", "Page number", "15")]
    )
    backfilled = page_identities.backfill_page_number_schema(
        conn,
        args.student_id,
        args.source_id,
        new_schema_version=new_version,
        from_schema_version=from_version,
        confirmed_at=datetime.now(UTC).isoformat(),
    )
    conn.close()
    print(
        f"{args.source_id}: schema version {from_version} -> {new_version} "
        f"(page_number, primary); backfilled {backfilled} confirmed page(s) from "
        f"version {from_version}, no re-scan needed"
    )


if __name__ == "__main__":
    main()
