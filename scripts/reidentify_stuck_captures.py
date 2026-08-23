"""Re-run capture processing for every still-stuck capture of one source, using
the photo already on file -- no re-photographing.

For a capture whose page identity never resolved (needs_human_cause in the
identity-related causes, and no page_number), this re-reads the same stored image
bytes through the exact path a fresh photo takes (k12ta.ingest.normalize_
orientation -> evaluate_image_quality -> k12ta.pipeline.process_capture), so a
newer identity schema (e.g. a page-number-primary schema replacing a Day+Section
one that could never resolve -- docs/ROADMAP.md's M3.7) gets a real chance against
markers the model was never asked to look for the first time.

This spends real model-call quota, one call per stuck capture -- it is not
`replay_source` (which never calls the model, and only ever touches captures whose
identity already resolved). The old, still-stuck capture/session/problems rows are
never touched or deleted; this always creates a new capture_id, same as a real
re-photograph would. See k12ta.keys.app's pending-list dedup for why that's safe
to do without cluttering the parent-facing screen.

    python scripts/reidentify_stuck_captures.py dev-jahnvi summer_bridge
"""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

from k12ta.config import Settings, load_dotenv
from k12ta.ingest import capture as ingest_capture
from k12ta.llm import build_vision_model
from k12ta.pipeline.process import PipelineOutcome, process_capture
from k12ta.store import db, migrate
from k12ta.transcribe.vision_llm import VisionLLMTranscriber


def _find_stuck_captures(conn: sqlite3.Connection, student_id: str, source_id: str) -> list[str]:
    """Every capture, for this student/source, whose page identity never
    resolved: every graded_problems row it produced is still needs_human with
    an identity-related cause and no page_number. A capture with even one
    resolved item (a mixed photo) is left alone -- only genuinely, fully
    stuck captures are candidates for a fresh look."""
    cur = conn.execute(
        """
        SELECT DISTINCT gp.capture_id
        FROM graded_problems gp
        JOIN page_captures pc ON pc.student_id = gp.student_id AND pc.capture_id = gp.capture_id
        JOIN assignments a ON a.student_id = pc.student_id AND a.assignment_id = pc.assignment_id
        WHERE gp.student_id = ? AND a.source_id = ?
          AND gp.capture_id NOT IN (
              SELECT capture_id FROM graded_problems
              WHERE student_id = ? AND (page_number IS NOT NULL OR outcome != 'needs_human')
          )
        """,
        (student_id, source_id, student_id),
    )
    return [row[0] for row in cur.fetchall()]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("student_id")
    parser.add_argument("source_id")
    args = parser.parse_args()

    load_dotenv()
    settings = Settings.from_env()
    conn = db.connect(str(settings.data_dir / "k12ta.db"))
    migrate.apply_migrations(conn)

    stuck = _find_stuck_captures(conn, args.student_id, args.source_id)
    if not stuck:
        print(f"no stuck captures found for {args.source_id}")
        return

    vision_model = build_vision_model(settings)
    transcriber = VisionLLMTranscriber(
        vision_model, provider=settings.llm_provider, model=settings.llm_model
    )

    for old_capture_id in stuck:
        cur = conn.execute(
            "SELECT image_path, assignment_id FROM page_captures "
            "WHERE student_id = ? AND capture_id = ?",
            (args.student_id, old_capture_id),
        )
        row = cur.fetchone()
        image_path, assignment_id = row[0], row[1]
        image_bytes = ingest_capture.normalize_orientation(Path(image_path).read_bytes())
        verdict = ingest_capture.evaluate_image_quality(image_bytes, check_for_spread=True)
        if not verdict.accepted:
            print(f"{old_capture_id}: re-check rejected the stored photo ({verdict.reason})")
            continue

        outcome = process_capture(
            conn,
            settings,
            lambda: transcriber,
            args.student_id,
            assignment_id,
            image_bytes,
        )
        if isinstance(outcome, PipelineOutcome) and outcome.status.value == "graded":
            print(f"{old_capture_id}: re-processed as new capture, session {outcome.session_id}")
        else:
            reason = getattr(outcome, "failure_reason", None)
            print(f"{old_capture_id}: {outcome.status.value}{f' ({reason})' if reason else ''}")

    conn.close()


if __name__ == "__main__":
    main()
