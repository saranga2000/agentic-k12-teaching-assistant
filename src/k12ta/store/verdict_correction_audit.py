"""An append-only log of every time a graded_problems verdict changed by a
parent's hand, whichever of k12ta.store.sessions's three verdict-changing
functions produced it -- a first resolution of a row the grader refused to
call (apply_human_verdict), a parent's later correction of one it already did
call (correct_decided_verdict), or a dispute's resolution (overturn_dispute_
to_correct). Never updated, only inserted into -- a record of what happened,
not current state. Same shape and reasoning as k12ta.store.policy_override_
audit and k12ta.store.answer_key_audit.

Also the source docs/ROADMAP.md's M5 "fixture promotion" reads from: every row
here is a labelled disagreement between what the grader said and what a
parent said, which is exactly the ground truth docs/EVALS.md family 4 needs
and has no other free source of.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass


class VerdictCorrectionSource:
    """Which of k12ta.store.sessions's three functions wrote a given audit
    row -- a plain string constant set, not an Enum, so it round-trips
    through SQLite without a translation layer, same choice as every other
    free-text status column in this codebase (e.g. GradedProblemRow.outcome
    itself)."""

    NEEDS_HUMAN_RESOLUTION = "needs_human_resolution"
    DECIDED_VERDICT_CORRECTION = "decided_verdict_correction"
    DISPUTE_OVERTURNED = "dispute_overturned"


@dataclass(frozen=True)
class VerdictCorrectionAuditRow:
    student_id: str
    session_id: str
    capture_id: str
    problem_id: str
    corrected_at: str
    previous_outcome: str
    previous_needs_human_cause: str | None
    new_outcome: str
    previous_student_answer_raw: str
    new_student_answer_raw: str
    """Equal to previous_student_answer_raw when only the verdict changed --
    always a real string, never NULL, so "did the transcription change too"
    is a plain comparison rather than a NULL check."""
    source: str
    """One of VerdictCorrectionSource's constants."""


def insert_audit_row(conn: sqlite3.Connection, row: VerdictCorrectionAuditRow) -> None:
    conn.execute(
        """
        INSERT INTO verdict_correction_audit_log
            (student_id, session_id, capture_id, problem_id, corrected_at,
             previous_outcome, previous_needs_human_cause, new_outcome,
             previous_student_answer_raw, new_student_answer_raw, source)
        VALUES
            (:student_id, :session_id, :capture_id, :problem_id, :corrected_at,
             :previous_outcome, :previous_needs_human_cause, :new_outcome,
             :previous_student_answer_raw, :new_student_answer_raw, :source)
        """,
        vars(row),
    )
    conn.commit()


def list_for_problem(
    conn: sqlite3.Connection,
    student_id: str,
    session_id: str,
    capture_id: str,
    problem_id: str,
) -> list[VerdictCorrectionAuditRow]:
    """Every correction ever recorded for this exact row, oldest first -- used
    to decide whether a child should see a "this was corrected" notice
    (k12ta.respond.render) and, if so, to show the most recent one."""
    cur = conn.execute(
        """
        SELECT student_id, session_id, capture_id, problem_id, corrected_at,
               previous_outcome, previous_needs_human_cause, new_outcome,
               previous_student_answer_raw, new_student_answer_raw, source
        FROM verdict_correction_audit_log
        WHERE student_id = ? AND session_id = ? AND capture_id = ? AND problem_id = ?
        ORDER BY id
        """,
        (student_id, session_id, capture_id, problem_id),
    )
    return [VerdictCorrectionAuditRow(**dict(row)) for row in cur.fetchall()]


def list_for_source(
    conn: sqlite3.Connection, student_id: str, source_id: str
) -> list[VerdictCorrectionAuditRow]:
    """Every correction ever recorded for this source, oldest first -- the
    feed k12ta.evals.fixtures's promotion path and any future family-4 script
    read from."""
    cur = conn.execute(
        """
        SELECT v.student_id AS student_id, v.session_id AS session_id,
               v.capture_id AS capture_id, v.problem_id AS problem_id,
               v.corrected_at AS corrected_at, v.previous_outcome AS previous_outcome,
               v.previous_needs_human_cause AS previous_needs_human_cause,
               v.new_outcome AS new_outcome,
               v.previous_student_answer_raw AS previous_student_answer_raw,
               v.new_student_answer_raw AS new_student_answer_raw, v.source AS source
        FROM verdict_correction_audit_log v
        JOIN page_captures pc ON pc.student_id = v.student_id AND pc.capture_id = v.capture_id
        JOIN assignments a ON a.student_id = pc.student_id AND a.assignment_id = pc.assignment_id
        WHERE v.student_id = ? AND a.source_id = ?
        ORDER BY v.id
        """,
        (student_id, source_id),
    )
    return [VerdictCorrectionAuditRow(**dict(row)) for row in cur.fetchall()]
