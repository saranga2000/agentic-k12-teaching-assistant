"""Re-run grading for every already-resolved capture of one source, free.

Zero model calls -- k12ta.pipeline.process.replay_source only ever re-decides
each capture's already-stored transcription against the answer key as it
stands right now. Meant to be run after any answer-key correction or grading
change, against real photographed captures already on file: a repeatable
regression check that costs nothing after the captures were first ingested.

    python scripts/replay_source.py dev-jahnvi summer_bridge
"""

from __future__ import annotations

import argparse

from k12ta.config import Settings
from k12ta.pipeline.process import replay_source
from k12ta.store import db, migrate


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("student_id")
    parser.add_argument("source_id")
    args = parser.parse_args()

    settings = Settings.from_env()
    conn = db.connect(str(settings.data_dir / "k12ta.db"))
    migrate.apply_migrations(conn)
    summary = replay_source(conn, args.student_id, args.source_id)
    conn.close()
    print(f"Replayed {summary.captures_replayed} resolved captures for {summary.source_id}")


if __name__ == "__main__":
    main()
