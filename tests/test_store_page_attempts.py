"""k12ta.store.sessions.count_page_attempts: docs/ROADMAP.md's V1 "Attempts"
cap -- distinct captures that have ever resolved to one page number,
independent of k12ta.domain.attempts' own per-problem text-diff logic.
"""

from __future__ import annotations

import sqlite3

from k12ta.store import captures, content, db, migrate, sessions, students


def _migrated_connection() -> sqlite3.Connection:
    conn = db.connect(":memory:")
    migrate.apply_migrations(conn)
    return conn


def _seed_student_and_source(conn: sqlite3.Connection) -> None:
    students.insert_student(
        conn,
        students.StudentRow(
            student_id="s-marcus",
            display_name="Marcus",
            grade_level=7,
            state_code="CA",
            coach_name="Coach",
        ),
    )
    content.insert_content_source(
        conn,
        content.ContentSourceRow(
            student_id="s-marcus",
            source_id="summer_bridge",
            label="Summer bridge workbook",
            kind="workbook",
            subject="math",
            has_answer_key=True,
            graded_by_someone_else=False,
            default_mode="full",
            typical_session_minutes=30,
        ),
    )
    content.insert_assignment(
        conn,
        content.AssignmentRow(
            student_id="s-marcus",
            assignment_id="a-1",
            source_id="summer_bridge",
            created_at="2026-08-13T08:00:00+00:00",
        ),
    )


def _insert_graded(
    conn: sqlite3.Connection,
    *,
    capture_id: str,
    problem_id: str,
    outcome: str,
    page_number: int | None,
    new_capture: bool = True,
) -> None:
    session_id = f"sess-{capture_id}"
    if new_capture:
        captures.insert_page_capture(
            conn,
            captures.PageCaptureRow(
                student_id="s-marcus",
                capture_id=capture_id,
                assignment_id="a-1",
                captured_at="2026-08-13T08:00:00+00:00",
                image_path="/tmp/does-not-matter.jpg",
            ),
        )
        sessions.insert_session(
            conn,
            sessions.SessionRow(
                student_id="s-marcus",
                session_id=session_id,
                assignment_id="a-1",
                started_at="2026-08-13T08:00:00+00:00",
            ),
        )
    captures.insert_problem(
        conn,
        captures.ProblemRow(
            student_id="s-marcus",
            capture_id=capture_id,
            problem_id=problem_id,
            prompt_text="12 + 7",
            student_answer_raw="19",
            transcription_confidence=0.9,
        ),
    )
    sessions.insert_graded_problem(
        conn,
        sessions.GradedProblemRow(
            student_id="s-marcus",
            session_id=session_id,
            capture_id=capture_id,
            problem_id=problem_id,
            outcome=outcome,
            grader_confidence=0.9,
            page_number=page_number,
        ),
    )


def test_zero_when_the_page_has_never_been_captured() -> None:
    conn = _migrated_connection()
    _seed_student_and_source(conn)

    assert sessions.count_page_attempts(conn, "s-marcus", "summer_bridge", 15) == 0


def test_counts_one_capture_as_one_attempt() -> None:
    conn = _migrated_connection()
    _seed_student_and_source(conn)
    _insert_graded(conn, capture_id="c-1", problem_id="1", outcome="correct", page_number=15)

    assert sessions.count_page_attempts(conn, "s-marcus", "summer_bridge", 15) == 1


def test_multiple_problems_on_the_same_capture_count_as_one_attempt() -> None:
    conn = _migrated_connection()
    _seed_student_and_source(conn)
    _insert_graded(conn, capture_id="c-1", problem_id="1", outcome="correct", page_number=15)
    _insert_graded(
        conn,
        capture_id="c-1",
        problem_id="2",
        outcome="incorrect",
        page_number=15,
        new_capture=False,
    )

    assert sessions.count_page_attempts(conn, "s-marcus", "summer_bridge", 15) == 1


def test_three_distinct_captures_count_as_three_attempts() -> None:
    conn = _migrated_connection()
    _seed_student_and_source(conn)
    _insert_graded(conn, capture_id="c-1", problem_id="1", outcome="incorrect", page_number=15)
    _insert_graded(conn, capture_id="c-2", problem_id="1", outcome="incorrect", page_number=15)
    _insert_graded(conn, capture_id="c-3", problem_id="1", outcome="correct", page_number=15)

    assert sessions.count_page_attempts(conn, "s-marcus", "summer_bridge", 15) == 3


def test_a_different_page_number_is_not_counted() -> None:
    conn = _migrated_connection()
    _seed_student_and_source(conn)
    _insert_graded(conn, capture_id="c-1", problem_id="1", outcome="correct", page_number=15)
    _insert_graded(conn, capture_id="c-2", problem_id="1", outcome="correct", page_number=16)

    assert sessions.count_page_attempts(conn, "s-marcus", "summer_bridge", 15) == 1
    assert sessions.count_page_attempts(conn, "s-marcus", "summer_bridge", 16) == 1


def test_a_capture_with_no_resolved_page_number_is_never_counted() -> None:
    conn = _migrated_connection()
    _seed_student_and_source(conn)
    _insert_graded(conn, capture_id="c-1", problem_id="1", outcome="needs_human", page_number=None)

    assert sessions.count_page_attempts(conn, "s-marcus", "summer_bridge", 15) == 0
