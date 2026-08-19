"""Sessions and the graded problems produced within them."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class SessionRow:
    student_id: str
    session_id: str
    assignment_id: str
    started_at: str
    ended_at: str | None = None


def insert_session(conn: sqlite3.Connection, row: SessionRow) -> None:
    conn.execute(
        """
        INSERT INTO sessions (student_id, session_id, assignment_id, started_at, ended_at)
        VALUES (:student_id, :session_id, :assignment_id, :started_at, :ended_at)
        """,
        vars(row),
    )
    conn.commit()


def get_session(conn: sqlite3.Connection, student_id: str, session_id: str) -> SessionRow | None:
    cur = conn.execute(
        "SELECT * FROM sessions WHERE student_id = ? AND session_id = ?",
        (student_id, session_id),
    )
    row = cur.fetchone()
    return None if row is None else SessionRow(**dict(row))


@dataclass(frozen=True)
class GradedProblemRow:
    student_id: str
    session_id: str
    capture_id: str
    problem_id: str
    outcome: str
    grader_confidence: float
    expected_answer: str | None = None
    page_number: int | None = None
    """The page identity resolved at grading time, for k12ta.domain.attempts to
    recognise a later capture as another attempt at this same problem. NULL for
    most NEEDS_HUMAN causes; always set for CORRECT/INCORRECT, since
    k12ta.grading.answer_keys.get_entry cannot produce a verdict without one."""
    needs_human_cause: str | None = None
    needs_human_detail: str | None = None
    """Small JSON object, e.g. {"seen": ["Day"], "missing": ["Section"]}, using a
    schema's parent-facing labels -- populated only for causes whose message needs
    facts beyond the cause itself (PARTIAL_PAGE_MARKERS today). Decided once in
    k12ta.pipeline.process, never re-derived by a renderer -- same rule as
    diagnosis_skill_ids on this same row."""
    diagnosis_misconception_id: str | None = None
    diagnosis_explanation: str | None = None
    diagnosis_error_location: str | None = None
    diagnosis_skill_ids: tuple[str, ...] = ()


def insert_graded_problem(conn: sqlite3.Connection, row: GradedProblemRow) -> None:
    conn.execute(
        """
        INSERT INTO graded_problems
            (student_id, session_id, capture_id, problem_id, outcome, expected_answer,
             page_number, needs_human_cause, needs_human_detail, grader_confidence,
             diagnosis_misconception_id, diagnosis_explanation, diagnosis_error_location,
             diagnosis_skill_ids)
        VALUES
            (:student_id, :session_id, :capture_id, :problem_id, :outcome, :expected_answer,
             :page_number, :needs_human_cause, :needs_human_detail, :grader_confidence,
             :diagnosis_misconception_id, :diagnosis_explanation, :diagnosis_error_location,
             :diagnosis_skill_ids)
        """,
        {**vars(row), "diagnosis_skill_ids": json.dumps(list(row.diagnosis_skill_ids))},
    )
    conn.commit()


def update_graded_problem_after_identity_resolution(
    conn: sqlite3.Connection,
    *,
    student_id: str,
    session_id: str,
    capture_id: str,
    problem_id: str,
    outcome: str,
    expected_answer: str | None,
    page_number: int,
    needs_human_cause: str | None,
) -> None:
    """The shared regrade path: a problem that couldn't be graded at capture
    time because its page identity was unresolved later gets re-decided once
    more information arrives -- a student's constrained pick
    (k12ta.grading.page_identity.resolve_partial) most commonly, or a parent
    later adding a key for the page a pick just resolved onto. Updates the
    existing row in place rather than inserting a new one -- list_graded_
    attempts_for_source orders by the *capture's* own timestamp
    (page_captures.captured_at, never touched here), so this problem keeps
    its correct chronological position even if other captures of the same
    problem happened in the meantime: still the first attempt, never a
    second one, per k12ta.domain.attempts.

    `needs_human_cause` is a real parameter, not always cleared to NULL:
    resolving identity does not guarantee a key exists for the resolved page,
    so the re-decision can land on a *different* NEEDS_HUMAN cause
    (NO_KEY_FOR_PAGE, NEEDS_PERSON) rather than a definite grade. Always
    clears needs_human_detail, since PARTIAL_PAGE_MARKERS's seen/missing
    detail (the only cause that uses it) cannot apply once identity has
    resolved."""
    conn.execute(
        """
        UPDATE graded_problems
        SET outcome = :outcome, expected_answer = :expected_answer,
            page_number = :page_number, needs_human_cause = :needs_human_cause,
            needs_human_detail = NULL
        WHERE student_id = :student_id AND session_id = :session_id
            AND capture_id = :capture_id AND problem_id = :problem_id
        """,
        {
            "student_id": student_id,
            "session_id": session_id,
            "capture_id": capture_id,
            "problem_id": problem_id,
            "outcome": outcome,
            "expected_answer": expected_answer,
            "page_number": page_number,
            "needs_human_cause": needs_human_cause,
        },
    )
    conn.commit()


def list_graded_problems_for_session(
    conn: sqlite3.Connection, student_id: str, session_id: str
) -> list[GradedProblemRow]:
    cur = conn.execute(
        "SELECT * FROM graded_problems WHERE student_id = ? AND session_id = ? "
        "ORDER BY capture_id, problem_id",
        (student_id, session_id),
    )
    return [_row_to_graded(row) for row in cur.fetchall()]


def _row_to_graded(row: sqlite3.Row) -> GradedProblemRow:
    data = dict(row)
    data["diagnosis_skill_ids"] = tuple(json.loads(data["diagnosis_skill_ids"]))
    return GradedProblemRow(**data)


@dataclass(frozen=True)
class GradedAttemptRow:
    """One graded_problems row, widened with the identity and timing fields
    needed to recognise it as another attempt at the same problem as a row from
    a different capture. k12ta.domain.attempts is where that recognition
    happens -- this is only the fetch, no interpretation."""

    page_number: int
    problem_id: str
    outcome: str
    student_answer_raw: str
    captured_at: str
    capture_id: str


def list_graded_attempts_for_source(
    conn: sqlite3.Connection, student_id: str, source_id: str
) -> list[GradedAttemptRow]:
    """Every graded_problems row for this student and source whose page number
    resolved, across every session and capture, in chronological order. Rows
    that never resolved a page are excluded rather than grouped under a shared
    NULL key, which would incorrectly merge unrelated unresolved-page problems.
    Both k12ta.respond (per problem, at render time) and k12ta.keys (per source,
    for the parent-visible repeat count) group this by (page_number, problem_id)
    themselves -- grouping is plain data plumbing, not a repository concern, so
    it doesn't live here (see tests/test_store_scoping.py: every function in
    this module is conn-first and student_id-scoped, which a pure grouping
    helper with neither would fail)."""
    cur = conn.execute(
        """
        SELECT gp.page_number AS page_number, gp.problem_id AS problem_id,
               gp.outcome AS outcome, p.student_answer_raw AS student_answer_raw,
               pc.captured_at AS captured_at, gp.capture_id AS capture_id
        FROM graded_problems gp
        JOIN page_captures pc ON pc.student_id = gp.student_id AND pc.capture_id = gp.capture_id
        JOIN assignments a ON a.student_id = pc.student_id AND a.assignment_id = pc.assignment_id
        JOIN problems p ON p.student_id = gp.student_id AND p.capture_id = gp.capture_id
            AND p.problem_id = gp.problem_id
        WHERE gp.student_id = ? AND a.source_id = ? AND gp.page_number IS NOT NULL
        ORDER BY gp.page_number, gp.problem_id, pc.captured_at, gp.capture_id
        """,
        (student_id, source_id),
    )
    return [GradedAttemptRow(**dict(row)) for row in cur.fetchall()]
