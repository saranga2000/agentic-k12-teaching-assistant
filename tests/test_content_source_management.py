"""k12ta.store.content.update_content_source_label and delete_content_source:
the gap found 2026-08-22 running the real app on real data (docs/ROADMAP.md)
-- `seed_dev_data` creates sources a family may never use, and there was no
way to rename or remove one.
"""

from __future__ import annotations

import sqlite3

from k12ta.store import answer_keys, captures, content, db, migrate, students


def _migrated_connection() -> sqlite3.Connection:
    conn = db.connect(":memory:")
    migrate.apply_migrations(conn)
    return conn


def _seed_source(conn: sqlite3.Connection, source_id: str = "daily_fluency_drill") -> None:
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
            label="Daily timed fluency packet",
            kind="fluency_drill",
            subject="math",
            has_answer_key=False,
            graded_by_someone_else=False,
            default_mode="fluency",
            typical_session_minutes=10,
        ),
    )


def test_update_content_source_label_renames_it() -> None:
    conn = _migrated_connection()
    _seed_source(conn)

    content.update_content_source_label(conn, "s-marcus", "daily_fluency_drill", "Fluency (unused)")

    row = content.get_content_source(conn, "s-marcus", "daily_fluency_drill")
    assert row is not None
    assert row.label == "Fluency (unused)"


def test_delete_content_source_removes_an_untouched_source() -> None:
    conn = _migrated_connection()
    _seed_source(conn)

    deleted = content.delete_content_source(conn, "s-marcus", "daily_fluency_drill")

    assert deleted is True
    assert content.get_content_source(conn, "s-marcus", "daily_fluency_drill") is None


def test_delete_content_source_removes_empty_assignments_too() -> None:
    """An assignment row gets created every day a student opens the capture
    screen for a scheduled source, even before she ever takes a photo -- that
    alone must not block deleting an otherwise-untouched source."""
    conn = _migrated_connection()
    _seed_source(conn)
    content.insert_assignment(
        conn,
        content.AssignmentRow(
            student_id="s-marcus",
            assignment_id="a-1",
            source_id="daily_fluency_drill",
            created_at="2026-08-13T08:00:00+00:00",
        ),
    )

    deleted = content.delete_content_source(conn, "s-marcus", "daily_fluency_drill")

    assert deleted is True
    assert content.get_assignment(conn, "s-marcus", "a-1") is None


def test_delete_content_source_refuses_when_a_page_was_ever_photographed() -> None:
    conn = _migrated_connection()
    _seed_source(conn)
    content.insert_assignment(
        conn,
        content.AssignmentRow(
            student_id="s-marcus",
            assignment_id="a-1",
            source_id="daily_fluency_drill",
            created_at="2026-08-13T08:00:00+00:00",
        ),
    )
    captures.insert_page_capture(
        conn,
        captures.PageCaptureRow(
            student_id="s-marcus",
            capture_id="c-1",
            assignment_id="a-1",
            captured_at="2026-08-13T08:00:00+00:00",
            image_path="/tmp/does-not-matter.jpg",
        ),
    )

    deleted = content.delete_content_source(conn, "s-marcus", "daily_fluency_drill")

    assert deleted is False
    assert content.get_content_source(conn, "s-marcus", "daily_fluency_drill") is not None


def test_delete_content_source_refuses_when_an_answer_key_exists() -> None:
    conn = _migrated_connection()
    _seed_source(conn)
    answer_keys.upsert_entry(
        conn,
        answer_keys.AnswerKeyEntryRow(
            student_id="s-marcus",
            source_id="daily_fluency_drill",
            page_number=1,
            problem_number="1",
            answer_text="19",
            ungradeable_reason=None,
            confirmed_at="2026-08-13T09:00:00+00:00",
        ),
    )

    deleted = content.delete_content_source(conn, "s-marcus", "daily_fluency_drill")

    assert deleted is False
    assert content.get_content_source(conn, "s-marcus", "daily_fluency_drill") is not None


def test_delete_content_source_is_scoped_to_student() -> None:
    conn = _migrated_connection()
    _seed_source(conn)
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
            subject="math",
            has_answer_key=False,
            graded_by_someone_else=False,
            default_mode="fluency",
            typical_session_minutes=10,
        ),
    )

    content.delete_content_source(conn, "s-marcus", "daily_fluency_drill")

    assert content.get_content_source(conn, "s-priya", "daily_fluency_drill") is not None
