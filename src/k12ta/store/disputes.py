"""A child's own contest of an already-graded incorrect verdict -- Gap B/K/L
(docs/USER_WORKFLOWS.md). Distinct from k12ta.store.sessions.apply_human_verdict:
that resolves a row the grader itself refused to call (needs_human); this
resolves a row the grader DID call, that the child believes is wrong. One
dispute per graded_problems row, ever -- the household's own explicit decision
is that a parent's resolution is final, so filing a second dispute or
resolving one twice is refused here, not silently allowed.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class DisputeRow:
    student_id: str
    session_id: str
    capture_id: str
    problem_id: str
    reason: str
    disputed_at: str
    resolved_at: str | None = None
    resolution: str | None = None
    """"upheld" (the incorrect verdict stands) or "overturned" (the child was
    right; graded_problems.outcome is flipped in the same action by the
    caller -- see k12ta.store.sessions.overturn_dispute_to_correct). None
    while still open."""
    resolution_comment: str | None = None
    """Required at resolution time. None while still open."""


@dataclass(frozen=True)
class DisputedProblemRow:
    """One open dispute, widened with what a parent needs to see to judge it
    -- Gap K's prioritized queue, same shape and reasoning as
    k12ta.store.sessions.PendingProblemRow."""

    session_id: str
    capture_id: str
    problem_id: str
    prompt_text: str
    student_answer_raw: str
    expected_answer: str | None
    page_number: int | None
    reason: str
    disputed_at: str


def get(
    conn: sqlite3.Connection,
    student_id: str,
    session_id: str,
    capture_id: str,
    problem_id: str,
) -> DisputeRow | None:
    cur = conn.execute(
        """
        SELECT * FROM disputes
        WHERE student_id = ? AND session_id = ? AND capture_id = ? AND problem_id = ?
        """,
        (student_id, session_id, capture_id, problem_id),
    )
    row = cur.fetchone()
    return None if row is None else DisputeRow(**dict(row))


def file_dispute(
    conn: sqlite3.Connection,
    *,
    student_id: str,
    session_id: str,
    capture_id: str,
    problem_id: str,
    reason: str,
    disputed_at: str,
) -> bool:
    """Records a new dispute. Returns False, writing nothing, if one already
    exists for this exact item, open or resolved -- a child cannot dispute
    the same item twice, and a resolved dispute can never be reopened this
    way (the parent's word is final)."""
    if get(conn, student_id, session_id, capture_id, problem_id) is not None:
        return False
    conn.execute(
        """
        INSERT INTO disputes
            (student_id, session_id, capture_id, problem_id, reason, disputed_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (student_id, session_id, capture_id, problem_id, reason, disputed_at),
    )
    conn.commit()
    return True


def resolve(
    conn: sqlite3.Connection,
    *,
    student_id: str,
    session_id: str,
    capture_id: str,
    problem_id: str,
    resolution: str,
    resolution_comment: str,
    resolved_at: str,
) -> bool:
    """Records a parent's resolution. Returns False, writing nothing, if no
    open dispute exists for this item (already resolved, or never disputed
    at all) -- resolving twice is refused, not silently overwritten, the same
    "parent's word is final" rule enforced from the other direction."""
    existing = get(conn, student_id, session_id, capture_id, problem_id)
    if existing is None or existing.resolved_at is not None:
        return False
    conn.execute(
        """
        UPDATE disputes SET resolved_at = ?, resolution = ?, resolution_comment = ?
        WHERE student_id = ? AND session_id = ? AND capture_id = ? AND problem_id = ?
        """,
        (
            resolved_at,
            resolution,
            resolution_comment,
            student_id,
            session_id,
            capture_id,
            problem_id,
        ),
    )
    conn.commit()
    return True


def list_open_for_source(
    conn: sqlite3.Connection, student_id: str, source_id: str
) -> list[DisputedProblemRow]:
    """Every unresolved dispute for this source, oldest first -- Gap K's
    queue, joined the same way sessions.list_pending_for_source is."""
    cur = conn.execute(
        """
        SELECT d.session_id AS session_id, d.capture_id AS capture_id,
               d.problem_id AS problem_id, p.prompt_text AS prompt_text,
               p.student_answer_raw AS student_answer_raw,
               gp.expected_answer AS expected_answer, gp.page_number AS page_number,
               d.reason AS reason, d.disputed_at AS disputed_at
        FROM disputes d
        JOIN graded_problems gp ON gp.student_id = d.student_id AND gp.session_id = d.session_id
            AND gp.capture_id = d.capture_id AND gp.problem_id = d.problem_id
        JOIN page_captures pc ON pc.student_id = d.student_id AND pc.capture_id = d.capture_id
        JOIN assignments a ON a.student_id = pc.student_id AND a.assignment_id = pc.assignment_id
        JOIN problems p ON p.student_id = d.student_id AND p.capture_id = d.capture_id
            AND p.problem_id = d.problem_id
        WHERE d.student_id = ? AND a.source_id = ? AND d.resolved_at IS NULL
        ORDER BY d.disputed_at
        """,
        (student_id, source_id),
    )
    return [DisputedProblemRow(**dict(row)) for row in cur.fetchall()]
