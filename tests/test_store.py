"""Schema application and a full round trip of one session's graded work."""

from __future__ import annotations

import sqlite3

import pytest

from k12ta.store import captures, content, db, mastery, migrate, sessions, students

_EXPECTED_TABLES = {
    "schema_migrations",
    "students",
    "content_sources",
    "assignments",
    "page_captures",
    "problems",
    "sessions",
    "graded_problems",
    "skill_mastery_traces",
}


def _migrated_connection() -> sqlite3.Connection:
    conn = db.connect(":memory:")
    migrate.apply_migrations(conn)
    return conn


def test_schema_applies_cleanly_and_reapplying_is_a_no_op() -> None:
    conn = _migrated_connection()
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    }
    assert tables == _EXPECTED_TABLES
    assert migrate.apply_migrations(conn) == []


def _seed_marcus(conn: sqlite3.Connection) -> None:
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
            created_at="2026-08-12T08:00:00+00:00",
        ),
    )
    captures.insert_page_capture(
        conn,
        captures.PageCaptureRow(
            student_id="s-marcus",
            capture_id="c-1",
            assignment_id="a-1",
            captured_at="2026-08-12T08:05:00+00:00",
            image_path="/tmp/does-not-matter.jpg",
        ),
    )
    captures.insert_problem(
        conn,
        captures.ProblemRow(
            student_id="s-marcus",
            capture_id="c-1",
            problem_id="1",
            prompt_text="12 + 7",
            student_answer_raw="19",
            transcription_confidence=0.97,
            skill_ids=("integer-addition",),
            page_region=(10, 20, 200, 60),
        ),
    )
    sessions.insert_session(
        conn,
        sessions.SessionRow(
            student_id="s-marcus",
            session_id="sess-1",
            assignment_id="a-1",
            started_at="2026-08-12T08:05:00+00:00",
        ),
    )
    sessions.insert_graded_problem(
        conn,
        sessions.GradedProblemRow(
            student_id="s-marcus",
            session_id="sess-1",
            capture_id="c-1",
            problem_id="1",
            outcome="correct",
            grader_confidence=1.0,
            expected_answer="19",
        ),
    )
    mastery.upsert_skill_mastery(
        conn,
        mastery.SkillMasteryRow(
            student_id="s-marcus",
            skill_id="integer-addition",
            p_at_last_review=0.55,
            stability_days=2.0,
            last_reviewed_on="2026-08-12",
            review_count=1,
            correct_count=1,
        ),
    )


def test_round_trip_of_a_session_with_graded_problems() -> None:
    conn = _migrated_connection()
    _seed_marcus(conn)

    fetched_session = sessions.get_session(conn, "s-marcus", "sess-1")
    assert fetched_session is not None
    assert fetched_session.assignment_id == "a-1"

    graded = sessions.list_graded_problems_for_session(conn, "s-marcus", "sess-1")
    assert len(graded) == 1
    assert graded[0].outcome == "correct"
    assert graded[0].expected_answer == "19"

    problems = captures.list_problems_for_capture(conn, "s-marcus", "c-1")
    assert len(problems) == 1
    assert problems[0].skill_ids == ("integer-addition",)
    assert problems[0].page_region == (10, 20, 200, 60)

    trace = mastery.get_skill_mastery(conn, "s-marcus", "integer-addition")
    assert trace is not None
    assert trace.review_count == 1


def test_a_second_students_rows_never_surface_in_the_first_students_reads() -> None:
    conn = _migrated_connection()
    _seed_marcus(conn)
    students.insert_student(
        conn,
        students.StudentRow(
            student_id="s-priya",
            display_name="Priya",
            grade_level=4,
            state_code="CA",
            coach_name="Coach",
        ),
    )

    assert sessions.get_session(conn, "s-priya", "sess-1") is None
    assert sessions.list_graded_problems_for_session(conn, "s-priya", "sess-1") == []
    assert captures.list_problems_for_capture(conn, "s-priya", "c-1") == []
    assert mastery.get_skill_mastery(conn, "s-priya", "integer-addition") is None


def test_a_row_cannot_reference_another_students_parent_row() -> None:
    conn = _migrated_connection()
    _seed_marcus(conn)
    students.insert_student(
        conn,
        students.StudentRow(
            student_id="s-priya",
            display_name="Priya",
            grade_level=4,
            state_code="CA",
            coach_name="Coach",
        ),
    )

    with pytest.raises(sqlite3.IntegrityError):
        captures.insert_page_capture(
            conn,
            captures.PageCaptureRow(
                student_id="s-priya",
                capture_id="c-2",
                assignment_id="a-1",  # belongs to s-marcus, not s-priya
                captured_at="2026-08-12T09:00:00+00:00",
                image_path="/tmp/does-not-matter.jpg",
            ),
        )
