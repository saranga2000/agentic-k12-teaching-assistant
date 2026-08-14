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
             needs_human_cause, needs_human_detail, grader_confidence,
             diagnosis_misconception_id, diagnosis_explanation, diagnosis_error_location,
             diagnosis_skill_ids)
        VALUES
            (:student_id, :session_id, :capture_id, :problem_id, :outcome, :expected_answer,
             :needs_human_cause, :needs_human_detail, :grader_confidence,
             :diagnosis_misconception_id, :diagnosis_explanation, :diagnosis_error_location,
             :diagnosis_skill_ids)
        """,
        {**vars(row), "diagnosis_skill_ids": json.dumps(list(row.diagnosis_skill_ids))},
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
