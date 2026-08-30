"""Page captures and the problems transcribed from them.

`skill_ids` and `page_region` are stored as JSON text; the tuple shape is restored on
read so callers never see a raw JSON string.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class PageCaptureRow:
    student_id: str
    capture_id: str
    assignment_id: str
    captured_at: str
    image_path: str
    transcribe_failure_reason: str | None = None
    """Set only when this capture's transcribe step failed for a reason other
    than provider rate-limiting -- the exception's type and message, or a
    TranscriptionResult's own declared `failure` string. None for a capture
    still in flight, and None for a successful transcribe that simply found
    zero problems (see k12ta.pipeline.process -- that is not a failure).
    Written by record_transcribe_failure below, never at insert_page_capture
    time, since a capture's row exists before its transcribe step even
    starts. Never set alongside rate_limited_reason -- see that field."""
    rate_limited_reason: str | None = None
    """Set only when this capture's transcribe step failed specifically
    because the provider's own rate limit was exhausted (FailureKind.
    RATE_LIMITED) -- kept distinct from transcribe_failure_reason because it
    is not a transcription problem: the photo may be perfectly legible, the
    provider is just out of capacity. Written by record_rate_limited below.
    Never set alongside transcribe_failure_reason -- process_capture's
    failure branches are mutually exclusive by construction."""


def insert_page_capture(conn: sqlite3.Connection, row: PageCaptureRow) -> None:
    conn.execute(
        """
        INSERT INTO page_captures
            (student_id, capture_id, assignment_id, captured_at, image_path)
        VALUES
            (:student_id, :capture_id, :assignment_id, :captured_at, :image_path)
        """,
        vars(row),
    )
    conn.commit()


def get_page_capture(
    conn: sqlite3.Connection, student_id: str, capture_id: str
) -> PageCaptureRow | None:
    cur = conn.execute(
        "SELECT * FROM page_captures WHERE student_id = ? AND capture_id = ?",
        (student_id, capture_id),
    )
    row = cur.fetchone()
    return None if row is None else PageCaptureRow(**dict(row))


def record_transcribe_failure(
    conn: sqlite3.Connection, student_id: str, capture_id: str, reason: str
) -> None:
    """The one place a transcribe failure is written back to its capture row,
    so it is diagnosable after the fact instead of living only in a log line
    that does not survive a restart. Called from both of k12ta.pipeline.
    process.process_capture's failure paths -- an exception raised by
    get_transcriber/transcribe, and a TranscriptionResult with its own
    declared `failure` -- with the same string each already computes for
    PipelineOutcome.transcribe_failed."""
    conn.execute(
        "UPDATE page_captures SET transcribe_failure_reason = ? "
        "WHERE student_id = ? AND capture_id = ?",
        (reason, student_id, capture_id),
    )
    conn.commit()


def record_rate_limited(
    conn: sqlite3.Connection, student_id: str, capture_id: str, reason: str
) -> None:
    """Companion to record_transcribe_failure above, writing to
    rate_limited_reason instead -- called only when a TranscriptionResult's
    failure_kind is FailureKind.RATE_LIMITED, never for an ordinary
    transcribe failure. Kept as its own function, not a shared one with a
    column-name parameter, so each call site stays an unambiguous, greppable
    statement of which column it means to write."""
    conn.execute(
        "UPDATE page_captures SET rate_limited_reason = ? WHERE student_id = ? AND capture_id = ?",
        (reason, student_id, capture_id),
    )
    conn.commit()


@dataclass(frozen=True)
class ProblemRow:
    student_id: str
    capture_id: str
    problem_id: str
    prompt_text: str
    student_answer_raw: str
    transcription_confidence: float
    skill_ids: tuple[str, ...] = ()
    page_region: tuple[int, int, int, int] | None = None


def insert_problem(conn: sqlite3.Connection, row: ProblemRow) -> None:
    conn.execute(
        """
        INSERT INTO problems
            (student_id, capture_id, problem_id, prompt_text, student_answer_raw,
             transcription_confidence, skill_ids, page_region)
        VALUES
            (:student_id, :capture_id, :problem_id, :prompt_text, :student_answer_raw,
             :transcription_confidence, :skill_ids, :page_region)
        """,
        {
            **vars(row),
            "skill_ids": json.dumps(list(row.skill_ids)),
            "page_region": json.dumps(row.page_region) if row.page_region is not None else None,
        },
    )
    conn.commit()


def list_problems_for_capture(
    conn: sqlite3.Connection, student_id: str, capture_id: str
) -> list[ProblemRow]:
    cur = conn.execute(
        "SELECT * FROM problems WHERE student_id = ? AND capture_id = ? ORDER BY problem_id",
        (student_id, capture_id),
    )
    return [_row_to_problem(row) for row in cur.fetchall()]


def rename_problem_id(
    conn: sqlite3.Connection,
    student_id: str,
    capture_id: str,
    old_problem_id: str,
    new_problem_id: str,
) -> None:
    """Corrects a synthesized placeholder (k12ta.pipeline.process.
    AMBIGUOUS_PROBLEM_ID_PREFIX) once a parent reads the real printed
    question number off the photo -- the k12ta.keys route calling this
    re-decides the capture against the key afterward (regrade_capture_for_
    resolved_identity), the same way a page-identity pick already does.
    Updates `problems` before `graded_problems`: the latter's problem_id
    carries a foreign key into the former (migration 0001), and PRAGMA
    foreign_keys is ON (k12ta.store.db), so the referenced row must exist
    under the new id before the referencing one is repointed at it. Raises
    ValueError, not a bare constraint failure, when `new_problem_id` already
    names a different problem on this capture -- the exact ambiguity this
    function exists to resolve, never something to silently merge or
    overwrite."""
    collision = conn.execute(
        "SELECT 1 FROM problems WHERE student_id = ? AND capture_id = ? AND problem_id = ?",
        (student_id, capture_id, new_problem_id),
    ).fetchone()
    if collision is not None:
        raise ValueError(
            f"problem {new_problem_id!r} already exists on capture {capture_id!r}"
        )
    # Renaming a value a foreign key references (graded_problems.problem_id ->
    # problems.problem_id) trips PRAGMA foreign_keys = ON (k12ta.store.db) the
    # instant either table is updated alone -- whichever goes first, the other
    # still points at the old id. defer_foreign_keys pushes the check to this
    # transaction's commit instead of each statement, so the pair below is
    # checked only once both have landed; SQLite resets it to OFF automatically
    # after commit, so it never leaks into any other write on this connection.
    conn.execute("PRAGMA defer_foreign_keys = ON")
    conn.execute(
        "UPDATE problems SET problem_id = ? "
        "WHERE student_id = ? AND capture_id = ? AND problem_id = ?",
        (new_problem_id, student_id, capture_id, old_problem_id),
    )
    conn.execute(
        "UPDATE graded_problems SET problem_id = ? "
        "WHERE student_id = ? AND capture_id = ? AND problem_id = ?",
        (new_problem_id, student_id, capture_id, old_problem_id),
    )
    conn.commit()


def _row_to_problem(row: sqlite3.Row) -> ProblemRow:
    data = dict(row)
    data["skill_ids"] = tuple(json.loads(data["skill_ids"]))
    region = data["page_region"]
    data["page_region"] = tuple(json.loads(region)) if region is not None else None
    return ProblemRow(**data)
