"""Schema application and a full round trip of one session's graded work."""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

import pytest

from k12ta.store import (
    captures,
    content,
    db,
    mastery,
    migrate,
    quota,
    schedule,
    sessions,
    students,
)

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
    "weekly_default_sources",
    "daily_request_counts",
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


def test_list_content_sources_is_scoped_to_one_student() -> None:
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
    content.insert_content_source(
        conn,
        content.ContentSourceRow(
            student_id="s-priya",
            source_id="daily_fluency_drill",
            label="Daily timed fluency packet",
            kind="fluency_drill",
            subject="reading",
            has_answer_key=True,
            graded_by_someone_else=True,
            default_mode="fluency",
            typical_session_minutes=10,
        ),
    )

    marcus_sources = content.list_content_sources(conn, "s-marcus")

    assert [s.source_id for s in marcus_sources] == ["summer_bridge"]


def test_list_students_returns_every_student() -> None:
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

    all_students = students.list_students(conn)

    assert {s.student_id for s in all_students} == {"s-marcus", "s-priya"}


def test_weekly_default_source_round_trip_and_scoping() -> None:
    conn = _migrated_connection()
    _seed_marcus(conn)

    assert schedule.get_default_source(conn, "s-marcus", weekday=2) is None

    schedule.set_default_source(
        conn,
        schedule.WeeklyDefaultSourceRow(
            student_id="s-marcus", weekday=2, source_id="summer_bridge"
        ),
    )

    found = schedule.get_default_source(conn, "s-marcus", weekday=2)
    assert found is not None
    assert found.source_id == "summer_bridge"
    # A different weekday and a different student both see nothing.
    assert schedule.get_default_source(conn, "s-marcus", weekday=3) is None
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
    assert schedule.get_default_source(conn, "s-priya", weekday=2) is None


def test_weekly_default_source_cannot_reference_another_students_content_source() -> None:
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
        schedule.set_default_source(
            conn,
            schedule.WeeklyDefaultSourceRow(
                student_id="s-priya",  # summer_bridge belongs to s-marcus
                weekday=0,
                source_id="summer_bridge",
            ),
        )


def test_quota_count_starts_at_zero_and_increments_on_record() -> None:
    conn = _migrated_connection()
    on = date(2026, 8, 12)

    assert quota.get_count(conn, on) == 0

    first = quota.record_request(conn, on)
    second = quota.record_request(conn, on)

    assert first == 1
    assert second == 2
    assert quota.get_count(conn, on) == 2


def test_quota_count_is_scoped_to_one_calendar_day() -> None:
    conn = _migrated_connection()
    day_one = date(2026, 8, 12)
    day_two = date(2026, 8, 13)

    quota.record_request(conn, day_one)
    quota.record_request(conn, day_one)
    quota.record_request(conn, day_two)

    assert quota.get_count(conn, day_one) == 2
    assert quota.get_count(conn, day_two) == 1


def test_quota_count_persists_across_separate_connections_to_the_same_file(
    tmp_path: Path,
) -> None:
    db_path = str(tmp_path / "quota-test.db")
    on = date(2026, 8, 12)

    first_conn = db.connect(db_path)
    migrate.apply_migrations(first_conn)
    quota.record_request(first_conn, on)
    quota.record_request(first_conn, on)
    first_conn.close()

    second_conn = db.connect(db_path)
    migrate.apply_migrations(second_conn)
    assert quota.get_count(second_conn, on) == 2
    quota.record_request(second_conn, on)
    assert quota.get_count(second_conn, on) == 3
    second_conn.close()
