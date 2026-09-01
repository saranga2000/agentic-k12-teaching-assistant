"""k12ta.store.sessions.request_reminder and list_all_graded_for_source: the
student-side "my pages" view (k12ta.web.app) and its "remind my grown-up"
flag (migration 0019).
"""

from __future__ import annotations

import sqlite3

from k12ta.store import captures, content, db, migrate, sessions, students


def _migrated_connection() -> sqlite3.Connection:
    conn = db.connect(":memory:")
    migrate.apply_migrations(conn)
    return conn


def _seed_graded_problem(
    conn: sqlite3.Connection,
    *,
    capture_id: str,
    problem_id: str,
    outcome: str,
    page_number: int | None,
    needs_human_cause: str | None = None,
    session_id: str | None = None,
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
    resolved_session_id = session_id or f"sess-{capture_id}"
    sessions.insert_session(
        conn,
        sessions.SessionRow(
            student_id="s-marcus",
            session_id=resolved_session_id,
            assignment_id="a-1",
            started_at="2026-08-13T08:00:00+00:00",
        ),
    )
    sessions.insert_graded_problem(
        conn,
        sessions.GradedProblemRow(
            student_id="s-marcus",
            session_id=resolved_session_id,
            capture_id=capture_id,
            problem_id=problem_id,
            outcome=outcome,
            grader_confidence=0.9,
            page_number=page_number,
            needs_human_cause=needs_human_cause,
        ),
    )


def test_list_all_graded_for_source_includes_rows_with_no_resolved_page() -> None:
    conn = _migrated_connection()
    _seed_graded_problem(
        conn,
        capture_id="c-unresolved",
        problem_id="1",
        outcome="needs_human",
        page_number=None,
        needs_human_cause="unknown_page",
    )

    rows = sessions.list_all_graded_for_source(conn, "s-marcus", "summer_bridge")

    assert [r.capture_id for r in rows] == ["c-unresolved"]
    assert rows[0].page_number is None
    assert rows[0].reminder_requested_at is None


def test_list_all_graded_for_source_includes_correct_and_incorrect_rows() -> None:
    conn = _migrated_connection()
    _seed_graded_problem(
        conn, capture_id="c-correct", problem_id="1", outcome="correct", page_number=17
    )

    rows = sessions.list_all_graded_for_source(conn, "s-marcus", "summer_bridge")

    assert [(r.capture_id, r.outcome) for r in rows] == [("c-correct", "correct")]


def test_request_reminder_sets_the_timestamp() -> None:
    conn = _migrated_connection()
    _seed_graded_problem(
        conn,
        capture_id="c-waiting",
        problem_id="1",
        outcome="needs_human",
        page_number=15,
        needs_human_cause="no_key_for_page",
    )

    sessions.request_reminder(
        conn,
        student_id="s-marcus",
        session_id="sess-c-waiting",
        capture_id="c-waiting",
        problem_id="1",
        requested_at="2026-08-29T10:00:00+00:00",
    )

    pending = sessions.list_pending_for_source(conn, "s-marcus", "summer_bridge")
    assert pending[0].reminder_requested_at == "2026-08-29T10:00:00+00:00"


def test_request_reminder_can_be_retapped() -> None:
    conn = _migrated_connection()
    _seed_graded_problem(
        conn,
        capture_id="c-waiting",
        problem_id="1",
        outcome="needs_human",
        page_number=15,
        needs_human_cause="no_key_for_page",
    )

    sessions.request_reminder(
        conn,
        student_id="s-marcus",
        session_id="sess-c-waiting",
        capture_id="c-waiting",
        problem_id="1",
        requested_at="2026-08-29T10:00:00+00:00",
    )
    sessions.request_reminder(
        conn,
        student_id="s-marcus",
        session_id="sess-c-waiting",
        capture_id="c-waiting",
        problem_id="1",
        requested_at="2026-08-29T11:00:00+00:00",
    )

    pending = sessions.list_pending_for_source(conn, "s-marcus", "summer_bridge")
    assert pending[0].reminder_requested_at == "2026-08-29T11:00:00+00:00"


def test_get_graded_problem_returns_the_row() -> None:
    conn = _migrated_connection()
    _seed_graded_problem(conn, capture_id="c-1", problem_id="1", outcome="correct", page_number=17)

    row = sessions.get_graded_problem(conn, "s-marcus", "sess-c-1", "c-1", "1")

    assert row is not None
    assert row.outcome == "correct"


def test_get_graded_problem_returns_none_for_an_unknown_row() -> None:
    conn = _migrated_connection()
    _seed_graded_problem(conn, capture_id="c-1", problem_id="1", outcome="correct", page_number=17)

    assert sessions.get_graded_problem(conn, "s-marcus", "sess-c-1", "c-1", "nope") is None


def test_correct_decided_verdict_flips_an_already_decided_row() -> None:
    conn = _migrated_connection()
    _seed_graded_problem(conn, capture_id="c-1", problem_id="1", outcome="correct", page_number=17)

    sessions.correct_decided_verdict(
        conn,
        student_id="s-marcus",
        session_id="sess-c-1",
        capture_id="c-1",
        problem_id="1",
        outcome="incorrect",
    )

    row = sessions.get_graded_problem(conn, "s-marcus", "sess-c-1", "c-1", "1")
    assert row is not None
    assert row.outcome == "incorrect"
