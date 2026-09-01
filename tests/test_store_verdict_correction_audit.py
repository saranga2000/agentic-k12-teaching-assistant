"""k12ta.store.verdict_correction_audit: docs/ROADMAP.md's M5 audit trail for
a parent's correction of a graded_problems verdict. No test hits the network
-- this is pure store logic.
"""

from __future__ import annotations

import sqlite3

from k12ta.store import captures, content, db, migrate, sessions, students
from k12ta.store import verdict_correction_audit as audit


def _migrated_connection() -> sqlite3.Connection:
    conn = db.connect(":memory:")
    migrate.apply_migrations(conn)
    return conn


def _seed_graded_problem(
    conn: sqlite3.Connection,
    *,
    capture_id: str = "c-1",
    problem_id: str = "1",
    outcome: str = "needs_human",
    needs_human_cause: str | None = "needs_person",
    session_id: str = "sess-1",
    source_id: str = "summer_bridge",
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
            source_id=source_id,
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
            source_id=source_id,
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
            outcome=outcome,
            grader_confidence=0.9,
            page_number=17,
            needs_human_cause=needs_human_cause,
        ),
    )


def _row(**overrides: object) -> audit.VerdictCorrectionAuditRow:
    defaults: dict[str, object] = {
        "student_id": "s-marcus",
        "session_id": "sess-1",
        "capture_id": "c-1",
        "problem_id": "1",
        "corrected_at": "2026-08-31T21:00:00+00:00",
        "previous_outcome": "needs_human",
        "previous_needs_human_cause": "needs_person",
        "new_outcome": "correct",
        "previous_student_answer_raw": "19",
        "new_student_answer_raw": "19",
        "source": audit.VerdictCorrectionSource.NEEDS_HUMAN_RESOLUTION,
    }
    defaults.update(overrides)
    return audit.VerdictCorrectionAuditRow(**defaults)  # type: ignore[arg-type]


def test_insert_and_list_for_problem_round_trips_every_field() -> None:
    conn = _migrated_connection()
    _seed_graded_problem(conn)

    audit.insert_audit_row(conn, _row())

    rows = audit.list_for_problem(conn, "s-marcus", "sess-1", "c-1", "1")
    assert len(rows) == 1
    assert rows[0] == _row()


def test_list_for_problem_is_empty_when_nothing_was_ever_corrected() -> None:
    conn = _migrated_connection()
    _seed_graded_problem(conn)

    assert audit.list_for_problem(conn, "s-marcus", "sess-1", "c-1", "1") == []


def test_list_for_problem_orders_oldest_first() -> None:
    conn = _migrated_connection()
    _seed_graded_problem(conn)

    audit.insert_audit_row(
        conn, _row(corrected_at="2026-08-31T21:00:00+00:00", new_outcome="correct")
    )
    audit.insert_audit_row(
        conn,
        _row(
            corrected_at="2026-08-31T22:00:00+00:00",
            previous_outcome="correct",
            new_outcome="incorrect",
            source=audit.VerdictCorrectionSource.DECIDED_VERDICT_CORRECTION,
        ),
    )

    rows = audit.list_for_problem(conn, "s-marcus", "sess-1", "c-1", "1")
    assert [r.new_outcome for r in rows] == ["correct", "incorrect"]


def test_list_for_source_joins_through_capture_and_assignment() -> None:
    conn = _migrated_connection()
    _seed_graded_problem(conn, source_id="summer_bridge")
    audit.insert_audit_row(conn, _row())

    rows = audit.list_for_source(conn, "s-marcus", "summer_bridge")
    assert len(rows) == 1
    assert rows[0].problem_id == "1"


def test_list_for_source_is_empty_for_a_different_source() -> None:
    conn = _migrated_connection()
    _seed_graded_problem(conn, source_id="summer_bridge")
    audit.insert_audit_row(conn, _row())

    assert audit.list_for_source(conn, "s-marcus", "other_source") == []
