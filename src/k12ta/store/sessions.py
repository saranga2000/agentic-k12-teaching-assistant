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
    unsimplified: bool = False
    """CORRECT only, and only when the match required comparing fraction
    values rather than exact strings (k12ta.grading.needs_human.decide) --
    e.g. "2/6" against a key of "1/3". See GradeDecision.unsimplified."""
    diagnosis_misconception_id: str | None = None
    diagnosis_explanation: str | None = None
    diagnosis_error_location: str | None = None
    diagnosis_skill_ids: tuple[str, ...] = ()


def insert_graded_problem(conn: sqlite3.Connection, row: GradedProblemRow) -> None:
    conn.execute(
        """
        INSERT INTO graded_problems
            (student_id, session_id, capture_id, problem_id, outcome, expected_answer,
             page_number, needs_human_cause, needs_human_detail, unsimplified,
             grader_confidence, diagnosis_misconception_id, diagnosis_explanation,
             diagnosis_error_location, diagnosis_skill_ids)
        VALUES
            (:student_id, :session_id, :capture_id, :problem_id, :outcome, :expected_answer,
             :page_number, :needs_human_cause, :needs_human_detail, :unsimplified,
             :grader_confidence, :diagnosis_misconception_id, :diagnosis_explanation,
             :diagnosis_error_location, :diagnosis_skill_ids)
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
    unsimplified: bool = False,
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
            needs_human_detail = NULL, unsimplified = :unsimplified
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
            "unsimplified": unsimplified,
        },
    )
    conn.commit()


def apply_human_verdict(
    conn: sqlite3.Connection,
    *,
    student_id: str,
    session_id: str,
    capture_id: str,
    problem_id: str,
    outcome: str,
) -> None:
    """A parent's direct verdict on a row the grader deliberately refused to
    call itself -- today, only ANSWER_DIFFERS_FROM_KEY (a non-numeric answer
    that differs from the key, which might still be a valid alternate name;
    see k12ta.grading.needs_human.decide). Not a re-decision through decide()
    -- there is nothing new to re-derive, only a person's judgment to record.
    expected_answer and page_number are left as they were; only the verdict
    and the now-resolved needs_human fields change."""
    conn.execute(
        """
        UPDATE graded_problems
        SET outcome = :outcome, needs_human_cause = NULL, needs_human_detail = NULL
        WHERE student_id = :student_id AND session_id = :session_id
            AND capture_id = :capture_id AND problem_id = :problem_id
        """,
        {
            "student_id": student_id,
            "session_id": session_id,
            "capture_id": capture_id,
            "problem_id": problem_id,
            "outcome": outcome,
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
    data["unsimplified"] = bool(data["unsimplified"])
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


@dataclass(frozen=True)
class PendingProblemRow:
    """One graded_problems row still needs_human, widened with what a parent
    surface needs to show and act on it: the actual question and answer (so
    "what is this" doesn't require a second lookup), whether a page was
    resolved, why it's waiting, and when it was captured (for a parent to
    judge how long something has been sitting). Grouping by cause is plain
    data plumbing, done by the caller -- same reasoning as GradedAttemptRow
    above, not a repository concern."""

    session_id: str
    capture_id: str
    problem_id: str
    prompt_text: str
    student_answer_raw: str
    page_number: int | None
    needs_human_cause: str | None
    """None only for a row graded before this column existed (migration 0006)
    -- genuinely unknown, not a guess dressed up as one. See k12ta.respond.
    render.UNKNOWN_CAUSE_MESSAGE for the same honesty applied to the student-
    facing render of this same case."""
    captured_at: str
    expected_answer: str | None = None
    """The key's own answer, set only for ANSWER_DIFFERS_FROM_KEY -- what a
    parent needs to see side by side with student_answer_raw to judge it."""


def list_pending_for_source(
    conn: sqlite3.Connection, student_id: str, source_id: str
) -> list[PendingProblemRow]:
    """Every graded_problems row for this source still needs_human, across
    every session and capture, in capture order. The parent-facing "what's
    waiting" list (k12ta.keys) reads this and groups it by cause; the regrade-
    trigger route re-decides the no_key_for_page subset once a key exists."""
    cur = conn.execute(
        """
        SELECT gp.session_id AS session_id, gp.capture_id AS capture_id,
               gp.problem_id AS problem_id, p.prompt_text AS prompt_text,
               p.student_answer_raw AS student_answer_raw, gp.page_number AS page_number,
               gp.needs_human_cause AS needs_human_cause, pc.captured_at AS captured_at,
               gp.expected_answer AS expected_answer
        FROM graded_problems gp
        JOIN page_captures pc ON pc.student_id = gp.student_id AND pc.capture_id = gp.capture_id
        JOIN assignments a ON a.student_id = pc.student_id AND a.assignment_id = pc.assignment_id
        JOIN problems p ON p.student_id = gp.student_id AND p.capture_id = gp.capture_id
            AND p.problem_id = gp.problem_id
        WHERE gp.student_id = ? AND a.source_id = ? AND gp.outcome = 'needs_human'
        ORDER BY pc.captured_at, gp.capture_id, gp.problem_id
        """,
        (student_id, source_id),
    )
    return [PendingProblemRow(**dict(row)) for row in cur.fetchall()]


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


@dataclass(frozen=True)
class ResolvedCaptureRow:
    """One distinct capture, for one source, whose page identity is already
    known -- the unit k12ta.pipeline.process.replay_source iterates. A capture
    with more than one distinct page_number across its rows would be a bug
    elsewhere (one photograph is one physical page); this takes whichever one
    SQL happens to return first rather than guessing which is right."""

    capture_id: str
    session_id: str
    page_number: int


def list_resolved_captures_for_source(
    conn: sqlite3.Connection, student_id: str, source_id: str
) -> list[ResolvedCaptureRow]:
    """Every distinct capture for this student and source that already has a
    resolved page_number, across every session -- regardless of outcome
    (correct, incorrect, or still needs_human for some other reason, e.g.
    no_key_for_page). What k12ta.pipeline.process.replay_source loops over to
    re-decide every one of them from its already-stored transcription, never
    the model, after an answer key or grading-logic change."""
    cur = conn.execute(
        """
        SELECT DISTINCT gp.capture_id AS capture_id, gp.session_id AS session_id,
               gp.page_number AS page_number
        FROM graded_problems gp
        JOIN page_captures pc ON pc.student_id = gp.student_id AND pc.capture_id = gp.capture_id
        JOIN assignments a ON a.student_id = pc.student_id AND a.assignment_id = pc.assignment_id
        WHERE gp.student_id = ? AND a.source_id = ? AND gp.page_number IS NOT NULL
        ORDER BY pc.captured_at, gp.capture_id
        """,
        (student_id, source_id),
    )
    return [ResolvedCaptureRow(**dict(row)) for row in cur.fetchall()]
