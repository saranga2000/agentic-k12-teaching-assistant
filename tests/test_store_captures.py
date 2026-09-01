"""k12ta.store.captures.rename_problem_id: a parent correcting a synthesized
AMBIGUOUS_PROBLEM_ID_PREFIX placeholder (k12ta.pipeline.process) once she
reads the real printed question number off the photo.
"""

from __future__ import annotations

import sqlite3

import pytest

from k12ta.store import captures, content, db, migrate, sessions, students


def _migrated_connection() -> sqlite3.Connection:
    conn = db.connect(":memory:")
    migrate.apply_migrations(conn)
    return conn


def _seed_graded_problem(
    conn: sqlite3.Connection, *, capture_id: str, problem_id: str, session_id: str = "sess-1"
) -> None:
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
    sessions.insert_session(
        conn,
        sessions.SessionRow(
            student_id="s-marcus",
            session_id=session_id,
            assignment_id="a-1",
            started_at="2026-08-13T08:00:00+00:00",
        ),
    )
    sessions.insert_graded_problem(
        conn,
        sessions.GradedProblemRow(
            student_id="s-marcus",
            session_id=session_id,
            capture_id=capture_id,
            problem_id=problem_id,
            outcome="needs_human",
            grader_confidence=0.0,
            needs_human_cause="ambiguous_problem_id",
        ),
    )


def test_rename_problem_id_updates_both_problems_and_graded_problems() -> None:
    conn = _migrated_connection()
    _seed_graded_problem(conn, capture_id="c-1", problem_id="_ambiguous_0")

    captures.rename_problem_id(conn, "s-marcus", "c-1", "_ambiguous_0", "4")

    problems = captures.list_problems_for_capture(conn, "s-marcus", "c-1")
    assert [p.problem_id for p in problems] == ["4"]

    graded = sessions.list_graded_problems_for_session(conn, "s-marcus", "sess-1")
    assert [g.problem_id for g in graded] == ["4"]


def test_rename_problem_id_refuses_a_collision_with_a_real_problem() -> None:
    conn = _migrated_connection()
    _seed_graded_problem(conn, capture_id="c-1", problem_id="_ambiguous_0")
    captures.insert_problem(
        conn,
        captures.ProblemRow(
            student_id="s-marcus",
            capture_id="c-1",
            problem_id="4",
            prompt_text="5 + 5",
            student_answer_raw="10",
            transcription_confidence=0.9,
        ),
    )

    with pytest.raises(ValueError, match="already exists"):
        captures.rename_problem_id(conn, "s-marcus", "c-1", "_ambiguous_0", "4")

    # Refused, not partially applied.
    problems = {p.problem_id for p in captures.list_problems_for_capture(conn, "s-marcus", "c-1")}
    assert problems == {"_ambiguous_0", "4"}


def test_get_problem_returns_the_row() -> None:
    conn = _migrated_connection()
    _seed_graded_problem(conn, capture_id="c-1", problem_id="1")

    row = captures.get_problem(conn, "s-marcus", "c-1", "1")

    assert row is not None
    assert row.student_answer_raw == "19"


def test_get_problem_returns_none_for_an_unknown_problem() -> None:
    conn = _migrated_connection()
    _seed_graded_problem(conn, capture_id="c-1", problem_id="1")

    assert captures.get_problem(conn, "s-marcus", "c-1", "nope") is None
