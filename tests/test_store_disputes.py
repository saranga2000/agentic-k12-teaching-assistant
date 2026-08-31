"""Gap B/K/L (docs/USER_WORKFLOWS.md): a child's contest of an already-graded
incorrect verdict. No test hits the network -- this is pure store logic.
"""

from __future__ import annotations

import sqlite3

from k12ta.store import captures as store_captures
from k12ta.store import content, db, disputes, migrate, sessions, students


def _migrated_connection() -> sqlite3.Connection:
    conn = db.connect(":memory:")
    migrate.apply_migrations(conn)
    return conn


def _seed_graded_problem(conn: sqlite3.Connection, *, outcome: str = "incorrect") -> None:
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
    store_captures.insert_page_capture(
        conn,
        store_captures.PageCaptureRow(
            student_id="s-marcus",
            capture_id="c-1",
            assignment_id="a-1",
            captured_at="2026-08-13T08:00:00+00:00",
            image_path="/tmp/does-not-matter.jpg",
        ),
    )
    store_captures.insert_problem(
        conn,
        store_captures.ProblemRow(
            student_id="s-marcus",
            capture_id="c-1",
            problem_id="1",
            prompt_text="12 + 7",
            student_answer_raw="18",
            transcription_confidence=0.9,
        ),
    )
    sessions.insert_session(
        conn,
        sessions.SessionRow(
            student_id="s-marcus",
            session_id="sess-1",
            assignment_id="a-1",
            started_at="2026-08-13T08:00:00+00:00",
        ),
    )
    sessions.insert_graded_problem(
        conn,
        sessions.GradedProblemRow(
            student_id="s-marcus",
            session_id="sess-1",
            capture_id="c-1",
            problem_id="1",
            outcome=outcome,
            grader_confidence=0.9,
            expected_answer="19",
            page_number=15,
        ),
    )


def test_get_returns_none_when_nothing_was_filed() -> None:
    conn = _migrated_connection()
    _seed_graded_problem(conn)

    assert disputes.get(conn, "s-marcus", "sess-1", "c-1", "1") is None


def test_file_dispute_records_it_and_refuses_a_second_one() -> None:
    conn = _migrated_connection()
    _seed_graded_problem(conn)

    first = disputes.file_dispute(
        conn,
        student_id="s-marcus",
        session_id="sess-1",
        capture_id="c-1",
        problem_id="1",
        reason="I carried the 1 correctly",
        disputed_at="2026-08-13T09:00:00+00:00",
    )
    assert first is True
    row = disputes.get(conn, "s-marcus", "sess-1", "c-1", "1")
    assert row is not None
    assert row.reason == "I carried the 1 correctly"
    assert row.resolved_at is None

    second = disputes.file_dispute(
        conn,
        student_id="s-marcus",
        session_id="sess-1",
        capture_id="c-1",
        problem_id="1",
        reason="a different reason",
        disputed_at="2026-08-13T09:05:00+00:00",
    )
    assert second is False
    # Unchanged -- the second attempt wrote nothing.
    unchanged = disputes.get(conn, "s-marcus", "sess-1", "c-1", "1")
    assert unchanged is not None
    assert unchanged.reason == "I carried the 1 correctly"


def test_resolve_refuses_when_nothing_was_ever_disputed() -> None:
    conn = _migrated_connection()
    _seed_graded_problem(conn)

    changed = disputes.resolve(
        conn,
        student_id="s-marcus",
        session_id="sess-1",
        capture_id="c-1",
        problem_id="1",
        resolution="upheld",
        resolution_comment="stands",
        resolved_at="2026-08-13T09:00:00+00:00",
    )

    assert changed is False


def test_resolve_records_the_decision_and_refuses_a_second_resolution() -> None:
    conn = _migrated_connection()
    _seed_graded_problem(conn)
    disputes.file_dispute(
        conn,
        student_id="s-marcus",
        session_id="sess-1",
        capture_id="c-1",
        problem_id="1",
        reason="I think I'm right",
        disputed_at="2026-08-13T09:00:00+00:00",
    )

    first = disputes.resolve(
        conn,
        student_id="s-marcus",
        session_id="sess-1",
        capture_id="c-1",
        problem_id="1",
        resolution="overturned",
        resolution_comment="You're right, good catch!",
        resolved_at="2026-08-13T10:00:00+00:00",
    )
    assert first is True
    row = disputes.get(conn, "s-marcus", "sess-1", "c-1", "1")
    assert row is not None
    assert row.resolved_at == "2026-08-13T10:00:00+00:00"
    assert row.resolution == "overturned"
    assert row.resolution_comment == "You're right, good catch!"

    # The parent's word is final -- a second resolution changes nothing.
    second = disputes.resolve(
        conn,
        student_id="s-marcus",
        session_id="sess-1",
        capture_id="c-1",
        problem_id="1",
        resolution="upheld",
        resolution_comment="actually never mind",
        resolved_at="2026-08-13T11:00:00+00:00",
    )
    assert second is False
    unchanged = disputes.get(conn, "s-marcus", "sess-1", "c-1", "1")
    assert unchanged is not None
    assert unchanged.resolution == "overturned"
    assert unchanged.resolution_comment == "You're right, good catch!"


def test_list_open_for_source_excludes_resolved_disputes_and_widens_the_row() -> None:
    conn = _migrated_connection()
    _seed_graded_problem(conn)
    disputes.file_dispute(
        conn,
        student_id="s-marcus",
        session_id="sess-1",
        capture_id="c-1",
        problem_id="1",
        reason="I think I'm right",
        disputed_at="2026-08-13T09:00:00+00:00",
    )

    [open_item] = disputes.list_open_for_source(conn, "s-marcus", "summer_bridge")
    assert open_item.prompt_text == "12 + 7"
    assert open_item.student_answer_raw == "18"
    assert open_item.expected_answer == "19"
    assert open_item.page_number == 15
    assert open_item.reason == "I think I'm right"

    disputes.resolve(
        conn,
        student_id="s-marcus",
        session_id="sess-1",
        capture_id="c-1",
        problem_id="1",
        resolution="upheld",
        resolution_comment="stands",
        resolved_at="2026-08-13T10:00:00+00:00",
    )

    assert disputes.list_open_for_source(conn, "s-marcus", "summer_bridge") == []
