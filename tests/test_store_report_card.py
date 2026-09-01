"""k12ta.store.sessions.report_card_counts: docs/ROADMAP.md's V1 "Report
cards" -- five buckets, computed from final verdicts, one count per logical
problem identity even when it was attempted more than once.
"""

from __future__ import annotations

import sqlite3

from k12ta.store import captures, content, db, migrate, sessions, students


def _migrated_connection() -> sqlite3.Connection:
    conn = db.connect(":memory:")
    migrate.apply_migrations(conn)
    return conn


def _seed_student_and_source(conn: sqlite3.Connection, *, source_id: str = "summer_bridge") -> None:
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


def _insert_graded(
    conn: sqlite3.Connection,
    *,
    capture_id: str,
    problem_id: str,
    outcome: str,
    page_number: int | None,
    captured_at: str = "2026-08-13T08:00:00+00:00",
    answered: bool = True,
    needs_human_cause: str | None = None,
) -> None:
    captures.insert_page_capture(
        conn,
        captures.PageCaptureRow(
            student_id="s-marcus",
            capture_id=capture_id,
            assignment_id="a-1",
            captured_at=captured_at,
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
    session_id = f"sess-{capture_id}"
    sessions.insert_session(
        conn,
        sessions.SessionRow(
            student_id="s-marcus",
            session_id=session_id,
            assignment_id="a-1",
            started_at=captured_at,
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
            needs_human_cause=needs_human_cause,
            answered=answered,
        ),
    )


def test_counts_one_of_each_bucket() -> None:
    conn = _migrated_connection()
    _seed_student_and_source(conn)
    _insert_graded(conn, capture_id="c-1", problem_id="1", outcome="correct", page_number=1)
    _insert_graded(
        conn, capture_id="c-2", problem_id="2", outcome="partially_correct", page_number=2
    )
    _insert_graded(conn, capture_id="c-3", problem_id="3", outcome="incorrect", page_number=3)
    _insert_graded(
        conn,
        capture_id="c-4",
        problem_id="4",
        outcome="needs_human",
        page_number=4,
        needs_human_cause="needs_person",
    )
    _insert_graded(
        conn,
        capture_id="c-5",
        problem_id="5",
        outcome="needs_human",
        page_number=5,
        needs_human_cause="needs_person",
        answered=False,
    )

    counts = sessions.report_card_counts(conn, "s-marcus", "summer_bridge")

    assert counts.correct == 1
    assert counts.partially_correct == 1
    assert counts.incorrect == 1
    assert counts.still_awaiting_review == 1
    assert counts.not_answered == 1


def test_not_answered_takes_priority_over_still_awaiting_review() -> None:
    """A blank answer is its own bucket, distinct from a real attempt that
    still needs a human -- even though both currently carry outcome=
    needs_human on the row."""
    conn = _migrated_connection()
    _seed_student_and_source(conn)
    _insert_graded(
        conn,
        capture_id="c-1",
        problem_id="1",
        outcome="needs_human",
        page_number=1,
        needs_human_cause="needs_person",
        answered=False,
    )

    counts = sessions.report_card_counts(conn, "s-marcus", "summer_bridge")

    assert counts.not_answered == 1
    assert counts.still_awaiting_review == 0


def test_a_problem_attempted_twice_counts_once_at_its_latest_state() -> None:
    """docs/ROADMAP.md's V1: counts come from FINAL verdicts, not one count
    per historical attempt -- same de-duplication k12ta.web.app.my_pages
    already applies for "tried N times" grouping."""
    conn = _migrated_connection()
    _seed_student_and_source(conn)
    _insert_graded(
        conn,
        capture_id="c-1",
        problem_id="1",
        outcome="incorrect",
        page_number=7,
        captured_at="2026-08-13T08:00:00+00:00",
    )
    _insert_graded(
        conn,
        capture_id="c-2",
        problem_id="1",
        outcome="correct",
        page_number=7,
        captured_at="2026-08-14T08:00:00+00:00",
    )

    counts = sessions.report_card_counts(conn, "s-marcus", "summer_bridge")

    assert counts.correct == 1
    assert counts.incorrect == 0


def test_a_row_with_no_resolved_page_number_is_counted_on_its_own() -> None:
    """No stable identity to de-duplicate by -- counted every time, same as
    my_pages leaves an unresolved capture ungrouped."""
    conn = _migrated_connection()
    _seed_student_and_source(conn)
    _insert_graded(
        conn,
        capture_id="c-1",
        problem_id="1",
        outcome="needs_human",
        page_number=None,
        needs_human_cause="unknown_page",
    )
    _insert_graded(
        conn,
        capture_id="c-2",
        problem_id="1",
        outcome="needs_human",
        page_number=None,
        needs_human_cause="unknown_page",
    )

    counts = sessions.report_card_counts(conn, "s-marcus", "summer_bridge")

    assert counts.still_awaiting_review == 2


def test_a_correction_is_reflected_as_the_final_verdict() -> None:
    conn = _migrated_connection()
    _seed_student_and_source(conn)
    _insert_graded(conn, capture_id="c-1", problem_id="1", outcome="incorrect", page_number=1)

    sessions.correct_decided_verdict(
        conn,
        student_id="s-marcus",
        session_id="sess-c-1",
        capture_id="c-1",
        problem_id="1",
        outcome="correct",
    )

    counts = sessions.report_card_counts(conn, "s-marcus", "summer_bridge")

    assert counts.correct == 1
    assert counts.incorrect == 0


def test_empty_source_is_all_zeros() -> None:
    conn = _migrated_connection()
    _seed_student_and_source(conn)

    counts = sessions.report_card_counts(conn, "s-marcus", "summer_bridge")

    assert counts.correct == 0
    assert counts.partially_correct == 0
    assert counts.incorrect == 0
    assert counts.not_answered == 0
    assert counts.still_awaiting_review == 0


def test_counts_are_scoped_to_one_source() -> None:
    conn = _migrated_connection()
    _seed_student_and_source(conn, source_id="summer_bridge")
    content.insert_content_source(
        conn,
        content.ContentSourceRow(
            student_id="s-marcus",
            source_id="rsm",
            label="RSM",
            kind="worksheet_packet",
            subject="math",
            has_answer_key=False,
            graded_by_someone_else=False,
            default_mode="full",
            typical_session_minutes=30,
        ),
    )
    content.insert_assignment(
        conn,
        content.AssignmentRow(
            student_id="s-marcus",
            assignment_id="a-2",
            source_id="rsm",
            created_at="2026-08-13T08:00:00+00:00",
        ),
    )
    _insert_graded(conn, capture_id="c-1", problem_id="1", outcome="correct", page_number=1)

    counts = sessions.report_card_counts(conn, "s-marcus", "rsm")

    assert counts.correct == 0
